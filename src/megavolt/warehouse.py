"""Writing fetched data into the local warehouse."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from itertools import chain

import psycopg

from megavolt.apcs import ProfilePoint
from megavolt.community import Member
from megavolt.entsoe import ImbalancePricePoint, LoadPoint, PricePoint
from megavolt.geosphere import WeatherPoint
from megavolt.readings import CommunityReading, Reading

DSN_ENV = "WAREHOUSE_DSN"

# Every ENTSO-E and weather table uses this shape. A published value can be revised and later
# return to an earlier value (A, B, A), so a row is stored when it differs from the LATEST stored
# row for its key: comparing against all of history would drop the third delivery and leave B
# winning. "Latest" is by received_at, so one clock sets it: the database. Only a test passes its
# own.
_INSERT_DAY_AHEAD_PRICE = """
INSERT INTO raw.day_ahead_price
    (interval_start, bidding_zone, resolution, price_eur_mwh, source, received_at, payload_hash)
SELECT %(interval_start)s::timestamptz, %(area)s::text, %(resolution)s::interval,
       %(price)s::double precision, 'entsoe', coalesce(%(received_at)s::timestamptz, now()),
       %(payload_hash)s::text
WHERE %(payload_hash)s::text IS DISTINCT FROM (
    SELECT payload_hash FROM raw.day_ahead_price
    WHERE bidding_zone = %(area)s::text AND interval_start = %(interval_start)s::timestamptz
    ORDER BY received_at DESC, payload_hash DESC
    LIMIT 1
)
ON CONFLICT DO NOTHING
"""

_INSERT_IMBALANCE_PRICE = """
INSERT INTO raw.imbalance_price
    (interval_start, control_area, category, doc_status, resolution, price_eur_mwh,
     source, received_at, payload_hash)
SELECT %(interval_start)s::timestamptz, %(area)s::text, %(category)s::text, %(doc_status)s::text,
       %(resolution)s::interval, %(price)s::double precision, 'entsoe',
       coalesce(%(received_at)s::timestamptz, now()), %(payload_hash)s::text
WHERE %(payload_hash)s::text IS DISTINCT FROM (
    SELECT payload_hash FROM raw.imbalance_price
    WHERE control_area = %(area)s::text
      AND interval_start = %(interval_start)s::timestamptz
      AND category = %(category)s::text
    ORDER BY received_at DESC, payload_hash DESC
    LIMIT 1
)
ON CONFLICT DO NOTHING
"""

_INSERT_ACTUAL_LOAD = """
INSERT INTO raw.actual_load
    (interval_start, bidding_zone, resolution, load_mw, source, received_at, payload_hash)
SELECT %(interval_start)s::timestamptz, %(area)s::text, %(resolution)s::interval,
       %(load)s::double precision, 'entsoe', coalesce(%(received_at)s::timestamptz, now()),
       %(payload_hash)s::text
WHERE %(payload_hash)s::text IS DISTINCT FROM (
    SELECT payload_hash FROM raw.actual_load
    WHERE bidding_zone = %(area)s::text AND interval_start = %(interval_start)s::timestamptz
    ORDER BY received_at DESC, payload_hash DESC
    LIMIT 1
)
ON CONFLICT DO NOTHING
"""

_INSERT_WEATHER = """
INSERT INTO raw.weather_observation
    (valid_at, dataset, latitude, longitude, parameter, value, source, received_at, payload_hash)
SELECT %(valid_at)s::timestamptz, %(dataset)s::text, %(latitude)s::double precision,
       %(longitude)s::double precision, %(parameter)s::text, %(value)s::double precision,
       'geosphere', coalesce(%(received_at)s::timestamptz, now()), %(payload_hash)s::text
WHERE %(payload_hash)s::text IS DISTINCT FROM (
    SELECT payload_hash FROM raw.weather_observation
    WHERE dataset = %(dataset)s::text
      AND latitude = %(latitude)s::double precision
      AND longitude = %(longitude)s::double precision
      AND parameter = %(parameter)s::text
      AND valid_at = %(valid_at)s::timestamptz
    ORDER BY received_at DESC, payload_hash DESC
    LIMIT 1
)
ON CONFLICT DO NOTHING
"""

_INSERT_LOAD_PROFILE = """
INSERT INTO raw.load_profile
    (profile_type, interval_start, profile_year, value, source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

_SELECT_LOAD_PROFILE = """
SELECT DISTINCT ON (profile_type, interval_start) profile_type, interval_start, value
FROM raw.load_profile
WHERE profile_year = %s AND profile_type = ANY(%s)
ORDER BY profile_type, interval_start, received_at DESC, payload_hash DESC
"""

_INSERT_METERING_POINT = """
INSERT INTO raw.metering_point
    (metering_point, valid_from, profile_type, segment, annual_kwh, meter_id,
     source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

_INSERT_METER_READING = """
INSERT INTO raw.meter_reading
    (metering_point, interval_start, consumption_kwh, allocated_kwh, meter_id,
     version, delivered_at, source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

_INSERT_COMMUNITY_INTERVAL = """
INSERT INTO raw.community_interval
    (interval_start, generation_kwh, consumption_kwh, version, delivered_at,
     source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

STREAM_SOURCE = "meter_stream"


class WarehouseError(RuntimeError):
    """Raised when the warehouse cannot be reached or asked to store nonsense."""


def _fingerprint(*fields: object) -> str:
    """Fingerprint one delivered value, so an identical re-delivery inserts nothing."""
    return hashlib.sha256("|".join(str(field) for field in fields).encode()).hexdigest()


def payload_hash(bidding_zone: str, point: PricePoint) -> str:
    """Fingerprint one delivered price; the same number for an hour and a quarter hour differ."""
    return _fingerprint(
        bidding_zone,
        point.interval_start.isoformat(),
        point.price_eur_mwh,
        point.resolution.total_seconds(),
    )


def imbalance_payload_hash(control_area: str, point: ImbalancePricePoint) -> str:
    """Fingerprint one delivered imbalance price. A status change alone is a new delivery."""
    return _fingerprint(
        control_area,
        point.interval_start.isoformat(),
        point.category,
        point.doc_status,
        point.price_eur_mwh,
    )


def load_payload_hash(bidding_zone: str, point: LoadPoint) -> str:
    """Fingerprint one delivered load value."""
    return _fingerprint(bidding_zone, point.interval_start.isoformat(), point.load_mw)


def weather_payload_hash(
    dataset: str, latitude: float, longitude: float, point: WeatherPoint
) -> str:
    """Fingerprint one delivered weather value."""
    return _fingerprint(
        dataset, latitude, longitude, point.parameter, point.valid_at.isoformat(), point.value
    )


def profile_payload_hash(point: ProfilePoint, profile_year: int) -> str:
    """Fingerprint one delivered profile value."""
    return _fingerprint(
        point.profile_type, point.interval_start.isoformat(), profile_year, point.value
    )


def reading_payload_hash(reading: Reading) -> str:
    """Fingerprint one delivered reading. A correction changes it; a replay does not."""
    return _fingerprint(
        reading.metering_point,
        reading.interval_start.isoformat(),
        reading.consumption_kwh,
        reading.allocated_kwh,
        reading.meter_id,
        reading.version,
        reading.delivered_at.isoformat(),
    )


def community_payload_hash(interval: CommunityReading) -> str:
    """Fingerprint one interval the community reported about itself."""
    return _fingerprint(
        interval.interval_start.isoformat(),
        interval.generation_kwh,
        interval.consumption_kwh,
        interval.version,
        interval.delivered_at.isoformat(),
    )


def dsn() -> str:
    """Read the warehouse connection string from the environment."""
    value = os.environ.get(DSN_ENV, "").strip()
    if not value:
        raise WarehouseError(f"{DSN_ENV} is not set. Copy .env.example to .env and fill it in.")
    return value


def day_ahead_rows(
    points: Sequence[PricePoint],
    bidding_zone: str,
    received_at: datetime | None = None,
) -> list[dict[str, object]]:
    """One parameter set per day-ahead price, for `_INSERT_DAY_AHEAD_PRICE`."""
    return [
        {
            "interval_start": point.interval_start,
            "area": bidding_zone,
            "resolution": point.resolution,
            "price": point.price_eur_mwh,
            "received_at": received_at,
            "payload_hash": payload_hash(bidding_zone, point),
        }
        for point in points
    ]


def store_day_ahead_prices(points: Sequence[PricePoint], bidding_zone: str) -> int:
    """Store price points and return how many rows were new."""
    if not points:
        raise WarehouseError("refusing to store an empty set of prices")

    rows = day_ahead_rows(points, bidding_zone)
    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_DAY_AHEAD_PRICE, rows)
        return cursor.rowcount


def imbalance_rows(
    points: Sequence[ImbalancePricePoint],
    control_area: str,
    received_at: datetime | None = None,
) -> list[dict[str, object]]:
    """One parameter set per imbalance price, for `_INSERT_IMBALANCE_PRICE`."""
    return [
        {
            "interval_start": point.interval_start,
            "area": control_area,
            "category": point.category,
            "doc_status": point.doc_status,
            "resolution": point.resolution,
            "price": point.price_eur_mwh,
            "received_at": received_at,
            "payload_hash": imbalance_payload_hash(control_area, point),
        }
        for point in points
    ]


def load_rows(
    points: Sequence[LoadPoint],
    bidding_zone: str,
    received_at: datetime | None = None,
) -> list[dict[str, object]]:
    """One parameter set per load value, for `_INSERT_ACTUAL_LOAD`."""
    return [
        {
            "interval_start": point.interval_start,
            "area": bidding_zone,
            "resolution": point.resolution,
            "load": point.load_mw,
            "received_at": received_at,
            "payload_hash": load_payload_hash(bidding_zone, point),
        }
        for point in points
    ]


def store_imbalance_prices(points: Sequence[ImbalancePricePoint], control_area: str) -> int:
    """Store the imbalance prices that changed since the last fetch and return how many did."""
    if not points:
        raise WarehouseError("refusing to store an empty set of imbalance prices")
    rows = imbalance_rows(points, control_area)
    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_IMBALANCE_PRICE, rows)
        return cursor.rowcount


def store_actual_load(points: Sequence[LoadPoint], bidding_zone: str) -> int:
    """Store the load values that changed since the last fetch and return how many did."""
    if not points:
        raise WarehouseError("refusing to store an empty set of load values")
    rows = load_rows(points, bidding_zone)
    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_ACTUAL_LOAD, rows)
        return cursor.rowcount


def weather_rows(
    points: Sequence[WeatherPoint],
    dataset: str,
    latitude: float,
    longitude: float,
    received_at: datetime | None = None,
) -> list[dict[str, object]]:
    """One parameter set per weather value, for `_INSERT_WEATHER`."""
    return [
        {
            "valid_at": point.valid_at,
            "dataset": dataset,
            "latitude": latitude,
            "longitude": longitude,
            "parameter": point.parameter,
            "value": point.value,
            "received_at": received_at,
            "payload_hash": weather_payload_hash(dataset, latitude, longitude, point),
        }
        for point in points
    ]


def store_weather(
    points: Sequence[WeatherPoint], dataset: str, latitude: float, longitude: float
) -> int:
    """Store the weather values that changed since the last fetch and return how many did."""
    if not points:
        raise WarehouseError("refusing to store an empty set of weather values")
    rows = weather_rows(points, dataset, latitude, longitude)
    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_WEATHER, rows)
        return cursor.rowcount


def store_load_profiles(points: Iterable[ProfilePoint], profile_year: int) -> int:
    """Store a year of profile values and return how many rows were new."""
    remaining = iter(points)
    first = next(remaining, None)
    if first is None:
        raise WarehouseError(f"refusing to store an empty set of {profile_year} profiles")

    def rows() -> Iterator[tuple[object, ...]]:
        for point in chain([first], remaining):
            yield (
                point.profile_type,
                point.interval_start,
                profile_year,
                point.value,
                "apcs",
                profile_payload_hash(point, profile_year),
            )

    # ponytail: executemany over ~946k rows takes tens of seconds. Once a year, so fine;
    # switch to COPY ... FROM STDIN if this ever runs more often.
    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_LOAD_PROFILE, rows())
        return cursor.rowcount


def load_profiles(profile_year: int, types: Sequence[str]) -> dict[str, dict[datetime, float]]:
    """Read whole profiles by type, newest delivery winning where a value was republished."""
    if not types:
        raise WarehouseError("refusing to read an empty set of profile types")

    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.execute(_SELECT_LOAD_PROFILE, (profile_year, list(types)))
        rows = cursor.fetchall()

    profiles: dict[str, dict[datetime, float]] = {name: {} for name in types}
    for profile_type, interval_start, value in rows:
        profiles[profile_type][interval_start] = value

    empty = sorted(name for name, series in profiles.items() if not series)
    if empty:
        raise WarehouseError(
            f"no {profile_year} profile stored for {', '.join(empty)} - run the apcs DAG first"
        )
    return profiles


def store_metering_points(points: Sequence[Member], valid_from: datetime) -> int:
    """Register our own metering points and return how many rows were new.

    A supplier never receives the other members' metering points, so handing one to the
    warehouse is a mistake worth failing on rather than a row worth writing.
    """
    if not points:
        raise WarehouseError("refusing to store an empty set of metering points")

    foreign = [point.metering_point for point in points if not point.ours]
    if foreign:
        raise WarehouseError(
            f"refusing to store {len(foreign)} metering points that are not our customers"
        )

    rows = [
        (
            point.metering_point,
            valid_from,
            point.profile_type,
            point.segment,
            point.annual_kwh,
            point.meter_id,
            "simulator",
            _fingerprint(
                point.metering_point,
                valid_from.isoformat(),
                point.profile_type,
                point.segment,
                point.annual_kwh,
                point.meter_id,
            ),
        )
        for point in points
    ]

    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_METERING_POINT, rows)
        return cursor.rowcount


def store_stream_batch(
    readings: Sequence[Reading], intervals: Sequence[CommunityReading]
) -> tuple[int, int]:
    """Store one consumer batch in a single transaction and return the new rows per table.

    One transaction, because a batch that half-lands and then has its offsets committed is
    exactly the silent loss the consumer's commit order exists to prevent.
    """
    if not readings and not intervals:
        raise WarehouseError("refusing to store an empty batch")

    reading_rows = [
        (
            reading.metering_point,
            reading.interval_start,
            reading.consumption_kwh,
            reading.allocated_kwh,
            reading.meter_id,
            reading.version,
            reading.delivered_at,
            STREAM_SOURCE,
            reading_payload_hash(reading),
        )
        for reading in readings
    ]
    interval_rows = [
        (
            interval.interval_start,
            interval.generation_kwh,
            interval.consumption_kwh,
            interval.version,
            interval.delivered_at,
            STREAM_SOURCE,
            community_payload_hash(interval),
        )
        for interval in intervals
    ]

    stored_readings = stored_intervals = 0
    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        if reading_rows:
            cursor.executemany(_INSERT_METER_READING, reading_rows)
            stored_readings = cursor.rowcount
        if interval_rows:
            cursor.executemany(_INSERT_COMMUNITY_INTERVAL, interval_rows)
            stored_intervals = cursor.rowcount
    return stored_readings, stored_intervals
