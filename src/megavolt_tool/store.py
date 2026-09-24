"""What the tool reads from the warehouse, as `megavolt_reader`: models dbt built, nothing else.

Every query is parameterised and read-only; the role could not write even if one were not, and
the connection string is refused unless it logs in as that role. The rows come out as plain
tuples the pricing engine takes as they are.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import psycopg

DSN_ENV = "TOOL_DSN"
READER = "megavolt_reader"
# The Austrian metering point identifier: AT, six-digit grid operator, five-digit postal code,
# twenty alphanumerics. Anything else is refused before it reaches a query.
METERING_POINT = re.compile(r"AT\d{11}[A-Z0-9]{20}")
# The engine reads two years of prices for its seasonal profile.
PRICE_YEARS = 2
log = logging.getLogger("megavolt_tool")


class StoreError(RuntimeError):
    """Raised when the warehouse cannot be read the way the tool expects."""


@dataclass(frozen=True, slots=True)
class Customer:
    """One of our customers as the registry knows them, pseudonymous by construction."""

    metering_point: str
    segment: str
    profile_type: str
    annual_kwh: float


_CUSTOMERS = """
SELECT metering_point, segment, profile_type, annual_kwh
FROM marts.dim_metering_point
ORDER BY segment, profile_type, metering_point
"""

_CUSTOMER = """
SELECT metering_point, segment, profile_type, annual_kwh
FROM marts.dim_metering_point
WHERE metering_point = %s
"""

_SETTLED = """
SELECT interval_start, bought_kwh, actual_kwh
FROM marts.fct_customer_cost
WHERE metering_point = %s AND is_settled
ORDER BY interval_start
"""

_PRICES = """
SELECT interval_start, price_eur_mwh
FROM staging.int_day_ahead_price_quarter_hour
WHERE interval_start >= now() - make_interval(years => %s)
ORDER BY interval_start
"""

# What a wrongly bought kWh cost over the day-ahead price, over every settled quarter hour,
# weighted by the volume that was wrong: a platform figure, never something a user enters.
_SPREAD = """
SELECT sum(abs(imbalance_kwh) * abs(imbalance_price_eur_mwh - day_ahead_price_eur_mwh) / 1000)
       / nullif(sum(abs(imbalance_kwh)), 0)
FROM marts.fct_customer_cost
WHERE is_settled AND imbalance_price_eur_mwh IS NOT NULL
"""


def dsn() -> str:
    """The reader's connection string, from the environment only, and only as the reader."""
    value = os.environ.get(DSN_ENV, "").strip()
    if not value:
        raise StoreError(f"{DSN_ENV} is not set; see .env.example")
    if urlsplit(value).username != READER:
        raise StoreError(f"{DSN_ENV} must log in as {READER}")
    return value


def valid_metering_point(value: str) -> str:
    """The identifier if it has the Austrian shape, else a ValueError (the route answers 422)."""
    if not METERING_POINT.fullmatch(value):
        raise ValueError("not a metering point identifier")
    return value


class Store:
    """The tool's read-only view of the warehouse."""

    def __init__(self, connection_string: str | None = None) -> None:
        self._dsn = connection_string or dsn()
        if urlsplit(self._dsn).username != READER:
            raise StoreError(f"the store must log in as {READER}")

    def _rows(self, query: str, parameters: tuple = ()) -> list[tuple]:
        try:
            with psycopg.connect(self._dsn) as connection, connection.cursor() as cursor:
                cursor.execute(query, parameters)
                return cursor.fetchall()
        except psycopg.Error as exc:
            # The driver's text can name the host and the table; the page gets a fixed line.
            log.warning("warehouse: %s", type(exc).__name__)
            raise StoreError("the warehouse is not reachable") from exc

    def customers(self) -> list[Customer]:
        """Every registered customer."""
        return [Customer(*row) for row in self._rows(_CUSTOMERS)]

    def customer(self, metering_point: str) -> Customer | None:
        """One customer, or None if the identifier is not ours."""
        rows = self._rows(_CUSTOMER, (valid_metering_point(metering_point),))
        return Customer(*rows[0]) if rows else None

    def settled(self, metering_point: str) -> list[tuple[datetime, float, float]]:
        """The customer's settled quarter hours: when, what we bought for them, what they took."""
        return self._rows(_SETTLED, (valid_metering_point(metering_point),))

    def prices(self) -> list[tuple[datetime, float]]:
        """Quarter-hourly day-ahead prices of the last years."""
        return self._rows(_PRICES, (PRICE_YEARS,))

    def spread_eur_kwh(self) -> float:
        """What a misbought kWh has cost over the day-ahead price, from the settlement."""
        (value,) = self._rows(_SPREAD)[0]
        if value is None:
            raise StoreError("no settled quarter hour with an imbalance price yet")
        return float(value)
