"""Writing fetched data into the local warehouse."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator, Sequence
from datetime import timedelta
from itertools import chain

import psycopg

from megavolt.apcs import ProfilePoint
from megavolt.entsoe import PricePoint

DSN_ENV = "WAREHOUSE_DSN"

_INSERT_DAY_AHEAD_PRICE = """
INSERT INTO raw.day_ahead_price
    (interval_start, bidding_zone, resolution, price_eur_mwh, source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

_INSERT_LOAD_PROFILE = """
INSERT INTO raw.load_profile
    (profile_type, interval_start, profile_year, value, source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


class WarehouseError(RuntimeError):
    """Raised when the warehouse cannot be reached or asked to store nonsense."""


def _fingerprint(*fields: object) -> str:
    """Fingerprint one delivered value, so an identical re-delivery inserts nothing."""
    return hashlib.sha256("|".join(str(field) for field in fields).encode()).hexdigest()


def payload_hash(bidding_zone: str, point: PricePoint) -> str:
    """Fingerprint one delivered price."""
    return _fingerprint(bidding_zone, point.interval_start.isoformat(), point.price_eur_mwh)


def profile_payload_hash(point: ProfilePoint, profile_year: int) -> str:
    """Fingerprint one delivered profile value."""
    return _fingerprint(
        point.profile_type, point.interval_start.isoformat(), profile_year, point.value
    )


def dsn() -> str:
    """Read the warehouse connection string from the environment."""
    value = os.environ.get(DSN_ENV, "").strip()
    if not value:
        raise WarehouseError(f"{DSN_ENV} is not set. Copy .env.example to .env and fill it in.")
    return value


def store_day_ahead_prices(
    points: Sequence[PricePoint], bidding_zone: str, resolution: timedelta
) -> int:
    """Store price points and return how many rows were new."""
    if not points:
        raise WarehouseError("refusing to store an empty set of prices")

    rows = [
        (
            point.interval_start,
            bidding_zone,
            resolution,
            point.price_eur_mwh,
            "entsoe",
            payload_hash(bidding_zone, point),
        )
        for point in points
    ]

    with psycopg.connect(dsn()) as connection, connection.cursor() as cursor:
        cursor.executemany(_INSERT_DAY_AHEAD_PRICE, rows)
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
