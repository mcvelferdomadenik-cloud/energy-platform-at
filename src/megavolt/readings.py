"""The message shape the simulator and the consumer both agree on.

One message carries one metering point's whole delivered day, keyed by the metering point, which
is how EDA sends documents: ~350 messages a day rather than 33,600. An array plus a UTC start
handles daylight saving for free, so a Vienna day is 92, 96 or 100 intervals long depending on
the date and nothing in the payload has to say so.

`null` in an array means **not delivered**. The consumer writes no row for it, so a missing
interval stays visibly missing instead of quietly becoming a zero that later averages into
somebody's bill.

Validation lives here rather than in the consumer because the producer has to obey the same rules
it will later be judged by. Everything a message can get wrong is checked before the database is
touched, so a bad message becomes a dead letter and never an aborted transaction.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

READINGS_TOPIC = "meter.readings.v1"
DEAD_LETTER_TOPIC = "meter.readings.dlq.v1"
BOOTSTRAP_ENV = "KAFKA_BOOTSTRAP_SERVERS"

READING_SCHEMA = "meter_reading_batch.v1"
COMMUNITY_SCHEMA = "community_interval_batch.v1"

RESOLUTION_MINUTES = 15
RESOLUTION = timedelta(minutes=RESOLUTION_MINUTES)

# A Vienna day is 23, 24 or 25 hours long, so a delivered day is one of exactly three lengths.
INTERVALS_PER_DAY = frozenset({92, 96, 100})

MAX_MESSAGE_BYTES = 64 * 1024
MAX_POINT_KWH = 1000.0
MAX_COMMUNITY_KWH = 100_000.0

# 33 characters: AT + six-digit grid operator + five-digit postal code + twenty alphanumeric.
METERING_POINT_PATTERN = re.compile(r"^AT[0-9A-Z]{31}$")
METER_ID_PATTERN = re.compile(r"^MV-\d{6}-[A-Z]$")

EARLIEST_INTERVAL = datetime(2015, 1, 1, tzinfo=UTC)
FUTURE_TOLERANCE = timedelta(days=30)


class MessageError(ValueError):
    """Raised when a message cannot be trusted. Always a dead letter, never a retry."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One quarter hour of one of our metering points, as delivered."""

    metering_point: str
    interval_start: datetime
    consumption_kwh: float
    allocated_kwh: float
    meter_id: str
    version: int
    delivered_at: datetime


@dataclass(frozen=True, slots=True)
class CommunityReading:
    """One quarter hour of what the community reports about itself."""

    interval_start: datetime
    generation_kwh: float
    consumption_kwh: float
    version: int
    delivered_at: datetime


def bootstrap_servers() -> str:
    """Read the broker address from the environment."""
    value = os.environ.get(BOOTSTRAP_ENV, "").strip()
    if not value:
        raise MessageError(f"{BOOTSTRAP_ENV} is not set. Copy .env.example to .env and fill it in.")
    return value


def reading_batch(
    metering_point: str,
    meter_id: str,
    version: int,
    delivered_at: datetime,
    interval_start: datetime,
    consumption: Sequence[float | None],
    allocated: Sequence[float | None],
) -> dict[str, object]:
    """Build one metering point's delivered day."""
    return {
        "schema": READING_SCHEMA,
        "metering_point": metering_point,
        "meter_id": meter_id,
        "version": version,
        "delivered_at": utc_text(delivered_at),
        "interval_start": utc_text(interval_start),
        "resolution_minutes": RESOLUTION_MINUTES,
        "consumption": list(consumption),
        "allocated": list(allocated),
    }


def community_batch(
    version: int,
    delivered_at: datetime,
    interval_start: datetime,
    generation: Sequence[float | None],
    consumption: Sequence[float | None],
) -> dict[str, object]:
    """Build the community's own delivered day: totals only, no member breakdown."""
    return {
        "schema": COMMUNITY_SCHEMA,
        "version": version,
        "delivered_at": utc_text(delivered_at),
        "interval_start": utc_text(interval_start),
        "resolution_minutes": RESOLUTION_MINUTES,
        "generation": list(generation),
        "consumption": list(consumption),
    }


def encode(batch: dict[str, object]) -> bytes:
    """Serialise a batch to bytes that are identical for identical content.

    Sorted keys and the compact separators are what make a replay produce the same bytes, and
    `allow_nan=False` refuses to emit the `NaN` literal, which is not JSON and which no database
    column should ever receive.
    """
    text = json.dumps(batch, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text.encode()


def decode(raw: bytes) -> dict[str, object]:
    """Parse one message, refusing anything oversized or not a JSON object."""
    if len(raw) > MAX_MESSAGE_BYTES:
        raise MessageError(f"message of {len(raw)} bytes exceeds {MAX_MESSAGE_BYTES}")
    try:
        payload = json.loads(raw.decode(), parse_constant=_refuse_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MessageError(f"not parseable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MessageError(f"expected a JSON object, got {type(payload).__name__}")
    return payload


def _refuse_constant(name: str) -> float:
    """Reject NaN and Infinity. One NaN in a double column poisons every aggregate over it."""
    raise MessageError(f"{name} is not an acceptable value")


def validate_reading_batch(payload: dict[str, object]) -> tuple[Reading, ...]:
    """Turn one validated message into the readings it delivers, skipping absent intervals."""
    _expect_schema(payload, READING_SCHEMA)
    metering_point = _identifier(payload, "metering_point", METERING_POINT_PATTERN)
    meter_id = _identifier(payload, "meter_id", METER_ID_PATTERN)
    version = _version(payload)
    delivered_at = _timestamp(payload, "delivered_at")
    start = _interval_start(payload)
    consumption = _amounts(payload, "consumption", MAX_POINT_KWH)
    allocated = _amounts(payload, "allocated", MAX_POINT_KWH)
    _expect_same_length(consumption, allocated, "consumption", "allocated")

    readings = []
    for position, (used, given) in enumerate(zip(consumption, allocated, strict=True)):
        if used is None and given is None:
            continue
        if used is None or given is None:
            raise MessageError(
                f"interval {position} delivers only one of consumption and allocated"
            )
        if given > used:
            raise MessageError(
                f"interval {position} allocates {given} of {used} consumed, which is impossible"
            )
        readings.append(
            Reading(
                metering_point=metering_point,
                interval_start=start + position * RESOLUTION,
                consumption_kwh=used,
                allocated_kwh=given,
                meter_id=meter_id,
                version=version,
                delivered_at=delivered_at,
            )
        )

    if not readings:
        raise MessageError("message delivers no intervals at all")
    return tuple(readings)


def validate_community_batch(payload: dict[str, object]) -> tuple[CommunityReading, ...]:
    """Turn the community's own message into the intervals it delivers."""
    _expect_schema(payload, COMMUNITY_SCHEMA)
    version = _version(payload)
    delivered_at = _timestamp(payload, "delivered_at")
    start = _interval_start(payload)
    generation = _amounts(payload, "generation", MAX_COMMUNITY_KWH)
    consumption = _amounts(payload, "consumption", MAX_COMMUNITY_KWH)
    _expect_same_length(generation, consumption, "generation", "consumption")

    intervals = []
    for position, (made, used) in enumerate(zip(generation, consumption, strict=True)):
        if made is None and used is None:
            continue
        if made is None or used is None:
            raise MessageError(
                f"interval {position} delivers only one of generation and consumption"
            )
        intervals.append(
            CommunityReading(
                interval_start=start + position * RESOLUTION,
                generation_kwh=made,
                consumption_kwh=used,
                version=version,
                delivered_at=delivered_at,
            )
        )

    if not intervals:
        raise MessageError("message delivers no intervals at all")
    return tuple(intervals)


def _expect_schema(payload: dict[str, object], expected: str) -> None:
    """Refuse a message that does not say what it is."""
    found = payload.get("schema")
    if found != expected:
        raise MessageError(f"expected schema {expected!r}, got {found!r}")
    resolution = payload.get("resolution_minutes")
    if resolution != RESOLUTION_MINUTES:
        raise MessageError(f"expected {RESOLUTION_MINUTES} minute intervals, got {resolution!r}")


def _identifier(payload: dict[str, object], field: str, pattern: re.Pattern[str]) -> str:
    """Refuse an identifier that does not have the shape it is supposed to have."""
    value = payload.get(field)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MessageError(f"{field} {value!r} is not a valid identifier")
    return value


def _version(payload: dict[str, object]) -> int:
    """A correction carries a higher version than what it corrects."""
    value = payload.get("version")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MessageError(f"version {value!r} is not a positive whole number")
    return value


def _timestamp(payload: dict[str, object], field: str) -> datetime:
    """Refuse a timestamp that is not timezone aware or not inside a sane window."""
    value = payload.get(field)
    if not isinstance(value, str):
        raise MessageError(f"{field} {value!r} is not a timestamp")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MessageError(f"{field} {value!r} is not a readable timestamp: {exc}") from exc
    if moment.tzinfo is None:
        raise MessageError(f"{field} {value!r} has no timezone, so its meaning is a guess")
    moment = moment.astimezone(UTC)
    if not EARLIEST_INTERVAL <= moment <= datetime.now(UTC) + FUTURE_TOLERANCE:
        raise MessageError(f"{field} {value!r} is outside the window we accept")
    return moment


def _interval_start(payload: dict[str, object]) -> datetime:
    """The first interval of the delivered day, which must sit on a quarter-hour boundary."""
    start = _timestamp(payload, "interval_start")
    if (start.minute, start.second, start.microsecond) not in {
        (0, 0, 0),
        (15, 0, 0),
        (30, 0, 0),
        (45, 0, 0),
    }:
        raise MessageError(f"interval_start {start.isoformat()} is not on a quarter hour")
    return start


def _amounts(payload: dict[str, object], field: str, cap: float) -> list[float | None]:
    """Refuse an array that is the wrong length or holds something that is not an amount."""
    values = payload.get(field)
    if not isinstance(values, list):
        raise MessageError(f"{field} is not an array")
    if len(values) not in INTERVALS_PER_DAY:
        raise MessageError(
            f"{field} has {len(values)} intervals, and a day is one of {sorted(INTERVALS_PER_DAY)}"
        )
    amounts: list[float | None] = []
    for position, value in enumerate(values):
        if value is None:
            amounts.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MessageError(f"{field}[{position}] {value!r} is not an amount")
        if not 0.0 <= value <= cap:
            raise MessageError(f"{field}[{position}] {value} is outside 0 to {cap} kWh")
        amounts.append(float(value))
    return amounts


def _expect_same_length(
    left: list[float | None], right: list[float | None], left_name: str, right_name: str
) -> None:
    """The two arrays describe the same intervals, so they must be the same length."""
    if len(left) != len(right):
        raise MessageError(
            f"{left_name} has {len(left)} intervals and {right_name} has {len(right)}"
        )


def utc_text(moment: datetime) -> str:
    """Render a timestamp the one way, so identical content encodes to identical bytes."""
    if moment.tzinfo is None:
        raise MessageError(f"refusing to send the naive timestamp {moment!r}")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def readings_of(payload: dict[str, object]) -> tuple[Reading, ...] | tuple[CommunityReading, ...]:
    """Validate a message of either kind, chosen by what it says it is."""
    schema = payload.get("schema")
    if schema == READING_SCHEMA:
        return validate_reading_batch(payload)
    if schema == COMMUNITY_SCHEMA:
        return validate_community_batch(payload)
    raise MessageError(f"unknown schema {schema!r}")
