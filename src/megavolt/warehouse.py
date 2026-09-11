"""Writing ENTSO-E results into the local warehouse."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from datetime import timedelta

import psycopg

from megavolt.entsoe import PricePoint

DSN_ENV = "WAREHOUSE_DSN"

_INSERT_DAY_AHEAD_PRICE = """
INSERT INTO raw.day_ahead_price
    (interval_start, bidding_zone, resolution, price_eur_mwh, source, payload_hash)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""


class WarehouseError(RuntimeError):
    """Raised when the warehouse cannot be reached or asked to store nonsense."""


def payload_hash(bidding_zone: str, point: PricePoint) -> str:
    """Fingerprint one delivered value, so an identical re-delivery inserts nothing."""
    fields = f"{bidding_zone}|{point.interval_start.isoformat()}|{point.price_eur_mwh}"
    return hashlib.sha256(fields.encode()).hexdigest()


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
