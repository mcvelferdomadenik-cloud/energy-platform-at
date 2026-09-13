"""Tests for the consumer's decisions. No broker, no database, no network.

The loop that talks to Redpanda is thin on purpose. What can be wrong lives in `classify`,
`partition` and `with_retries`, and those take plain bytes, so every case is testable here.
"""

from datetime import UTC, datetime

import pytest

from megavolt.consumer import (
    MAX_REASON_LENGTH,
    DeadLetter,
    classify,
    next_offsets,
    partition,
    with_retries,
)
from megavolt.readings import (
    MAX_MESSAGE_BYTES,
    CommunityReading,
    Reading,
    community_batch,
    encode,
    reading_batch,
)

POINT = "AT09999908430BUZOSQIZOKT57LP7GYW2"
KEY = POINT.encode()
DELIVERED = datetime(2025, 3, 3, 2, 30, tzinfo=UTC)
START = datetime(2025, 3, 1, 23, 0, tzinfo=UTC)


def reading_message(consumption=None, allocated=None, metering_point=POINT):
    """Bytes of one valid 96-interval reading batch, with a field replaced where a case needs it."""
    consumption = [0.1] * 96 if consumption is None else consumption
    allocated = [0.0] * 96 if allocated is None else allocated
    return encode(
        reading_batch(metering_point, "MV-000042-A", 1, DELIVERED, START, consumption, allocated)
    )


def community_message():
    """Bytes of one valid community aggregate."""
    return encode(community_batch(1, DELIVERED, START, [0.0] * 96, [130.0] * 96))


# --- classify: one message in, rows or a dead letter out -------------------------------------


def test_a_valid_reading_message_becomes_readings():
    result = classify(KEY, reading_message())
    assert len(result) == 96
    assert all(isinstance(row, Reading) for row in result)


def test_a_valid_community_message_becomes_community_intervals():
    result = classify(b"community", community_message())
    assert len(result) == 96
    assert all(isinstance(row, CommunityReading) for row in result)


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (b"x" * (MAX_MESSAGE_BYTES + 1), "exceeds"),
        (b'{"schema":"meter_reading_batch.v1","value":NaN}', "NaN"),
        (b"<html>503</html>", "not parseable JSON"),
        (b'{"schema":"something_else.v1"}', "unknown schema"),
    ],
    # Explicit ids: pytest otherwise names the case after its 64 KB value, and Windows refuses
    # an environment variable that long.
    ids=["oversize", "nan", "not-json", "unknown-schema"],
)
def test_a_message_that_cannot_be_trusted_becomes_a_dead_letter(value, reason):
    result = classify(KEY, value)
    assert isinstance(result, DeadLetter)
    assert reason in result.reason


def test_a_forged_metering_point_becomes_a_dead_letter_rather_than_a_row():
    result = classify(KEY, reading_message(metering_point="DE09999908430BUZOSQIZOKT57LP7GYW2"))
    assert isinstance(result, DeadLetter)
    assert "metering_point" in result.reason


def test_allocating_more_than_was_consumed_becomes_a_dead_letter():
    allocated = [0.0] * 96
    allocated[4] = 0.9
    result = classify(KEY, reading_message(allocated=allocated))
    assert isinstance(result, DeadLetter)
    assert "impossible" in result.reason


@pytest.mark.parametrize("empty", [None, b""])
def test_an_empty_message_becomes_a_dead_letter(empty):
    result = classify(KEY, empty)
    assert isinstance(result, DeadLetter)
    assert result.reason == "empty message"


def test_a_dead_letter_keeps_the_original_key_and_bytes_so_it_can_be_investigated():
    value = b"<html>503</html>"
    result = classify(KEY, value)
    assert result.key == KEY
    assert result.value == value


def test_a_dead_letter_reason_is_capped_because_it_echoes_what_the_sender_wrote():
    long_point = "AT" + "X" * 5000
    result = classify(KEY, reading_message(metering_point=long_point))
    assert isinstance(result, DeadLetter)
    assert len(result.reason) <= MAX_REASON_LENGTH


# --- partition: a batch in, nothing lost -----------------------------------------------------


def test_a_mixed_batch_is_split_by_table_and_nothing_is_lost():
    pairs = [
        (KEY, reading_message()),
        (b"community", community_message()),
        (KEY, b"not json"),
        (KEY, reading_message()),
        (KEY, None),
    ]
    readings, intervals, dead = partition(pairs)

    assert len(readings) == 2 * 96
    assert len(intervals) == 96
    assert len(dead) == 2


def test_a_batch_of_only_bad_messages_produces_no_rows():
    readings, intervals, dead = partition([(KEY, b"{}"), (KEY, b"[]")])
    assert readings == []
    assert intervals == []
    assert len(dead) == 2


def test_an_empty_batch_produces_nothing():
    assert partition([]) == ([], [], [])


# --- offsets: commit exactly what was processed -----------------------------------------------

TOPIC = "meter.readings.v1"


def test_the_committed_offset_is_one_past_the_last_processed_message():
    assert next_offsets([(TOPIC, 0, 0), (TOPIC, 0, 1), (TOPIC, 0, 135)]) == {(TOPIC, 0): 136}


def test_each_partition_gets_its_own_offset():
    positions = [(TOPIC, 0, 135), (TOPIC, 1, 123), (TOPIC, 2, 115)]
    assert next_offsets(positions) == {(TOPIC, 0): 136, (TOPIC, 1): 124, (TOPIC, 2): 116}


def test_the_order_messages_arrive_in_does_not_move_the_offset_backwards():
    assert next_offsets([(TOPIC, 0, 9), (TOPIC, 0, 3), (TOPIC, 0, 7)]) == {(TOPIC, 0): 10}


def test_a_batch_that_starts_mid_partition_commits_from_where_it_ended():
    assert next_offsets([(TOPIC, 1, 500), (TOPIC, 1, 501)]) == {(TOPIC, 1): 502}


def test_nothing_processed_means_nothing_to_commit():
    assert next_offsets([]) == {}


# --- retries: only the faults that can heal ---------------------------------------------------


class Unreachable(Exception):
    """Stands in for a database that is temporarily down."""


class Refused(Exception):
    """Stands in for a row the database will never accept."""


def failing(times, fault, result="stored"):
    """An action that raises `fault` for its first `times` calls, then succeeds."""
    calls = {"count": 0}

    def action():
        calls["count"] += 1
        if calls["count"] <= times:
            raise fault("no")
        return result

    return action, calls


def test_a_transient_fault_is_retried_until_it_succeeds():
    action, calls = failing(2, Unreachable)
    pauses = []
    assert with_retries(action, 5, 1.0, (Unreachable,), sleep=pauses.append) == "stored"
    assert calls["count"] == 3


def test_the_pause_doubles_between_attempts():
    action, _ = failing(3, Unreachable)
    pauses = []
    with_retries(action, 5, 1.0, (Unreachable,), sleep=pauses.append)
    assert pauses == [1.0, 2.0, 4.0]


def test_a_fault_that_outlasts_every_attempt_is_raised_not_swallowed():
    action, calls = failing(99, Unreachable)
    with pytest.raises(Unreachable):
        with_retries(action, 3, 1.0, (Unreachable,), sleep=lambda _: None)
    assert calls["count"] == 3


def test_a_fault_that_cannot_heal_is_raised_at_once_without_retrying():
    action, calls = failing(99, Refused)
    with pytest.raises(Refused):
        with_retries(action, 5, 1.0, (Unreachable,), sleep=lambda _: None)
    assert calls["count"] == 1
