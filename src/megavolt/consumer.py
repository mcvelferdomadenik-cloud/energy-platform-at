"""Read meter reading batches from Redpanda and write them to the warehouse.

The order of three steps is the whole design:

    dead letters flushed  ->  database committed  ->  offsets committed

A crash anywhere before the offset commit replays the batch, and `ON CONFLICT DO NOTHING`
absorbs the replay. Committing offsets any earlier would lose a delivery day with nothing to
show it was ever missing (T42).

A message that fails validation will never pass it, so it goes to the dead-letter topic and the
offset moves on (T43). A database that cannot be reached will come back, so that is retried,
and if it does not come back the consumer crashes loudly. The two are never handled alike.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial

from megavolt.readings import (
    DEAD_LETTER_TOPIC,
    READINGS_TOPIC,
    CommunityReading,
    MessageError,
    Reading,
    bootstrap_servers,
    decode,
    readings_of,
)

CONSUMER_GROUP = "megavolt-meter-consumer"
BATCH_SIZE = 500
POLL_SECONDS = 5.0

# How long --once waits for the group to hand it partitions before giving up.
ASSIGNMENT_WAIT_POLLS = 12

DATABASE_ATTEMPTS = 5
DATABASE_PAUSE_SECONDS = 2.0

# The reason echoes content the sender controls, so it is capped before it becomes a header.
MAX_REASON_LENGTH = 500

log = logging.getLogger("megavolt.consumer")


class ConsumerError(RuntimeError):
    """Raised when the consumer cannot guarantee a batch was handled, so it must stop."""


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """A message that can never be stored, and why."""

    key: bytes | None
    value: bytes | None
    reason: str


def classify(
    key: bytes | None, value: bytes | None
) -> tuple[Reading, ...] | tuple[CommunityReading, ...] | DeadLetter:
    """Turn one message into what it delivers, or into a dead letter. Never raises MessageError."""
    if not value:
        return DeadLetter(key, value, "empty message")
    try:
        return readings_of(decode(value))
    except MessageError as exc:
        return DeadLetter(key, value, str(exc)[:MAX_REASON_LENGTH])


def partition(
    pairs: Iterable[tuple[bytes | None, bytes | None]],
) -> tuple[list[Reading], list[CommunityReading], list[DeadLetter]]:
    """Split a batch into rows for each table and messages that belong in the dead-letter topic."""
    readings: list[Reading] = []
    intervals: list[CommunityReading] = []
    dead: list[DeadLetter] = []
    for key, value in pairs:
        result = classify(key, value)
        if isinstance(result, DeadLetter):
            dead.append(result)
        elif result and isinstance(result[0], Reading):
            readings.extend(result)
        else:
            intervals.extend(result)
    return readings, intervals, dead


def next_offsets(positions: Iterable[tuple[str, int, int]]) -> dict[tuple[str, int], int]:
    """The offset to commit per partition: one past the highest message actually processed.

    Committed explicitly rather than with a bare `commit()`, which commits whatever the client
    holds in its internal offset store. After a group's offsets were rewound, that store came
    back empty and the commit failed with `_NO_OFFSET` although every message had been read.
    Committing what was processed does not depend on that hidden state.
    """
    highest: dict[tuple[str, int], int] = {}
    for topic, partition_number, offset in positions:
        key = (topic, partition_number)
        highest[key] = max(highest.get(key, offset), offset)
    return {key: offset + 1 for key, offset in highest.items()}


def with_retries[T](
    action: Callable[[], T],
    attempts: int,
    pause: float,
    retryable: tuple[type[BaseException], ...],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run an action, retrying only the named faults with a doubling pause, then re-raising."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except retryable as exc:
            if attempt == attempts:
                raise
            wait = pause * 2 ** (attempt - 1)
            log.warning(
                "attempt %d of %d failed: %s; retrying in %.0f s", attempt, attempts, exc, wait
            )
            sleep(wait)
    raise ConsumerError("unreachable: the retry loop always returns or raises")


def _send_dead_letters(producer, letters: list[DeadLetter]) -> None:
    """Write every dead letter and wait until the broker has them all, or stop (T44)."""
    if not letters:
        return

    failures: list[str] = []

    def report(error, _message) -> None:
        if error is not None:
            failures.append(str(error))

    for letter in letters:
        log.warning("dead letter %r: %s", letter.key, letter.reason)
        producer.produce(
            DEAD_LETTER_TOPIC,
            value=letter.value,
            key=letter.key,
            headers=[("reason", letter.reason.encode())],
            on_delivery=report,
        )

    remaining = producer.flush(timeout=30)
    if remaining or failures:
        raise ConsumerError(
            f"{remaining} dead letters unacknowledged and {len(failures)} failed; "
            "refusing to move offsets past messages that were never recorded"
        )


def _commit(consumer, offsets: dict[tuple[str, int], int]) -> None:
    """Commit exactly the processed offsets, synchronously, and fail if any partition refused."""
    from confluent_kafka import KafkaException, TopicPartition

    requested = [
        TopicPartition(topic, number, offset) for (topic, number), offset in offsets.items()
    ]
    committed = consumer.commit(offsets=requested, asynchronous=False) or []
    refused = [part for part in committed if part.error is not None]
    if refused:
        raise KafkaException(refused[0].error)


def run(once: bool) -> None:
    """Consume until stopped, or with `once`, until the topic has nothing more to give."""
    import psycopg
    from confluent_kafka import Consumer, KafkaError, KafkaException, Producer

    from megavolt.warehouse import store_stream_batch

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap_servers(),
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    producer = Producer(
        {"bootstrap.servers": bootstrap_servers(), "enable.idempotence": True, "acks": "all"}
    )

    stopping = False

    def stop(signum, _frame) -> None:
        nonlocal stopping
        log.info("received signal %d, finishing the current batch", signum)
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    consumer.subscribe([READINGS_TOPIC])
    log.info("consuming %s as group %s", READINGS_TOPIC, CONSUMER_GROUP)

    unassigned_polls = 0
    try:
        while not stopping:
            messages = consumer.consume(num_messages=BATCH_SIZE, timeout=POLL_SECONDS)

            pairs = []
            positions = []
            for message in messages:
                error = message.error()
                if error is None:
                    pairs.append((message.key(), message.value()))
                    positions.append((message.topic(), message.partition(), message.offset()))
                elif error.code() != KafkaError._PARTITION_EOF:
                    raise KafkaException(error)

            if not pairs:
                # The first polls after subscribing are routinely empty while the group is still
                # joining. Empty only means "done" once partitions are actually assigned.
                if not consumer.assignment():
                    unassigned_polls += 1
                    if unassigned_polls >= ASSIGNMENT_WAIT_POLLS:
                        raise ConsumerError("no partitions assigned; is the broker reachable?")
                    continue
                unassigned_polls = 0
                if once:
                    log.info("topic drained, stopping")
                    break
                continue

            readings, intervals, dead = partition(pairs)
            _send_dead_letters(producer, dead)

            stored = (0, 0)
            if readings or intervals:
                stored = with_retries(
                    partial(store_stream_batch, readings, intervals),
                    attempts=DATABASE_ATTEMPTS,
                    pause=DATABASE_PAUSE_SECONDS,
                    retryable=(psycopg.OperationalError,),
                )

            _commit(consumer, next_offsets(positions))
            log.info(
                "batch of %d: %d readings (%d new), %d community intervals (%d new), "
                "%d dead letters",
                len(pairs),
                len(readings),
                stored[0],
                len(intervals),
                stored[1],
                len(dead),
            )
    finally:
        consumer.close()


def main() -> None:
    """Start the consumer."""
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s  %(message)s"
    )

    parser = argparse.ArgumentParser(description="Consume meter readings into the warehouse.")
    parser.add_argument(
        "--once", action="store_true", help="stop when the topic has nothing more to deliver"
    )
    run(parser.parse_args().once)


if __name__ == "__main__":
    main()
