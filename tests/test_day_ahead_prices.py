"""Day-ahead prices in the warehouse and in their quarter-hour model, against a real Postgres.

Every case runs inside a transaction that is always rolled back. Needs the running warehouse, so it
is skipped unless WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_day_ahead_prices.py
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import rendered  # noqa: E402
from megavolt.entsoe import PricePoint  # noqa: E402
from megavolt.warehouse import _INSERT_DAY_AHEAD_PRICE, day_ahead_rows, dsn  # noqa: E402

ZONE = "TEST-ZONE-NOT-REAL"
HOUR = datetime(2025, 3, 10, 11, 0, tzinfo=UTC)
FIRST, SECOND, THIRD = (datetime(2025, 4, day, 6, 0, tzinfo=UTC) for day in (1, 2, 3))


@pytest.fixture
def cursor():
    """A cursor whose work never reaches the warehouse."""
    connection = psycopg.connect(dsn())
    try:
        with connection.cursor() as cur:
            yield cur
    finally:
        connection.rollback()
        connection.close()


def deliver(cur, start: datetime, price: float, resolution: timedelta, received_at=FIRST) -> int:
    """Deliver one price and return how many rows that stored."""
    rows = day_ahead_rows([PricePoint(start, price, resolution)], ZONE, received_at)
    cur.executemany(_INSERT_DAY_AHEAD_PRICE, rows)
    return cur.rowcount


def quarter_hours(cur) -> list[tuple]:
    """What the quarter-hour model says about the test zone, in order."""
    cur.execute(
        "SELECT interval_start, price_eur_mwh"  # noqa: S608
        f" FROM ({rendered('int_day_ahead_price_quarter_hour')}) AS spread"
        " WHERE bidding_zone = %s ORDER BY interval_start",
        (ZONE,),
    )
    return cur.fetchall()


def test_an_hourly_price_applies_to_each_of_its_four_quarter_hours(cursor):
    deliver(cursor, HOUR, 98.23, timedelta(hours=1))
    assert quarter_hours(cursor) == [
        (HOUR + timedelta(minutes=minutes), 98.23) for minutes in (0, 15, 30, 45)
    ]


def test_a_quarter_hourly_price_passes_through_unchanged(cursor):
    deliver(cursor, HOUR, 200.66, timedelta(minutes=15))
    deliver(cursor, HOUR + timedelta(minutes=15), 187.67, timedelta(minutes=15))
    assert quarter_hours(cursor) == [(HOUR, 200.66), (HOUR + timedelta(minutes=15), 187.67)]


def test_two_neighbouring_hours_never_overlap(cursor):
    deliver(cursor, HOUR, 98.23, timedelta(hours=1))
    deliver(cursor, HOUR + timedelta(hours=1), 97.0, timedelta(hours=1))
    spread = quarter_hours(cursor)
    assert len(spread) == len({start for start, _ in spread}) == 8
    assert [price for _, price in spread] == [98.23] * 4 + [97.0] * 4


def test_a_price_revised_back_to_its_first_value_ends_on_that_value(cursor):
    hour = timedelta(hours=1)
    assert deliver(cursor, HOUR, 98.23, hour, FIRST) == 1
    assert deliver(cursor, HOUR, 101.5, hour, SECOND) == 1
    # The revision wins while it is the latest, so a wrong sort order cannot pass by luck.
    assert {price for _, price in quarter_hours(cursor)} == {101.5}
    assert deliver(cursor, HOUR, 98.23, hour, THIRD) == 1
    assert {price for _, price in quarter_hours(cursor)} == {98.23}


def test_fetching_an_unchanged_price_again_stores_nothing(cursor):
    assert deliver(cursor, HOUR, 98.23, timedelta(hours=1), FIRST) == 1
    assert deliver(cursor, HOUR, 98.23, timedelta(hours=1), SECOND) == 0


def test_the_database_refuses_a_price_that_is_not_a_number(cursor):
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, HOUR, float("nan"), timedelta(hours=1))


def test_an_hour_first_stored_under_a_quarter_hour_label_is_put_right_by_the_next_fetch(cursor):
    quarter, hour = timedelta(minutes=15), timedelta(hours=1)
    assert deliver(cursor, HOUR, 98.23, quarter, FIRST) == 1
    assert len(quarter_hours(cursor)) == 1
    # Same number, other resolution: a different delivery, so it is stored, and being later it wins.
    assert deliver(cursor, HOUR, 98.23, hour, SECOND) == 1
    assert quarter_hours(cursor) == [
        (HOUR + timedelta(minutes=minutes), 98.23) for minutes in (0, 15, 30, 45)
    ]


def test_a_quarter_hour_covered_twice_takes_the_price_received_last(cursor):
    quarter, hour = timedelta(minutes=15), timedelta(hours=1)
    deliver(cursor, HOUR, 98.23, hour, FIRST)
    deliver(cursor, HOUR + quarter, 120.0, quarter, SECOND)
    assert quarter_hours(cursor) == [
        (HOUR, 98.23),
        (HOUR + quarter, 120.0),
        (HOUR + 2 * quarter, 98.23),
        (HOUR + 3 * quarter, 98.23),
    ]
    # And the other way round: an hour received after a quarter hour covers it again.
    deliver(cursor, HOUR, 99.0, hour, THIRD)
    assert {price for _, price in quarter_hours(cursor)} == {99.0}


def test_the_database_refuses_a_resolution_that_would_blow_up_or_erase_a_price(cursor):
    for absurd in (timedelta(days=365), timedelta(0)):
        with pytest.raises(psycopg.errors.CheckViolation):
            deliver(cursor, HOUR, 98.23, absurd)
        cursor.connection.rollback()


def test_the_database_refuses_anything_off_the_quarter_hour_grid(cursor):
    for resolution in (timedelta(minutes=20), timedelta(minutes=50)):
        with pytest.raises(psycopg.errors.CheckViolation):
            deliver(cursor, HOUR, 98.23, resolution)
        cursor.connection.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, HOUR + timedelta(minutes=7), 98.23, timedelta(minutes=15))
