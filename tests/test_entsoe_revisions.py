"""Revised ENTSO-E values, proved against a real Postgres.

ENTSO-E changes imbalance prices and load after publishing them, and a value can return to an
earlier one. These tests run the real insert statements and the real staging models inside a
transaction that is always rolled back. Needs the running warehouse, so it is skipped unless
WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_entsoe_revisions.py
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import rendered  # noqa: E402
from megavolt.entsoe import ImbalancePricePoint, LoadPoint  # noqa: E402
from megavolt.warehouse import (  # noqa: E402
    _INSERT_ACTUAL_LOAD,
    _INSERT_IMBALANCE_PRICE,
    dsn,
    imbalance_rows,
    load_rows,
)

AREA = "TEST-AREA-NOT-REAL"
QUARTER = timedelta(minutes=15)
INTERVAL = datetime(2025, 3, 30, 23, 0, tzinfo=UTC)
FIRST, SECOND, THIRD = (datetime(2025, 4, day, 6, 0, tzinfo=UTC) for day in (1, 2, 3))


@pytest.fixture
def cursor():
    """A cursor whose work never reaches the warehouse."""
    # Never commits: raw is append-only for this role, so a committed test row could not be removed.
    connection = psycopg.connect(dsn(), autocommit=False)
    try:
        with connection.cursor() as cur:
            yield cur
    finally:
        connection.rollback()
        connection.close()


def deliver_price(cur, price: float, status: str, received_at: datetime) -> int:
    """Deliver one long-direction price and return how many rows that stored."""
    point = ImbalancePricePoint(INTERVAL, "A04", price, status, QUARTER)
    cur.executemany(_INSERT_IMBALANCE_PRICE, imbalance_rows([point], AREA, received_at))
    return cur.rowcount


def winning_price(cur) -> tuple[float, str]:
    """What the staging model says the price is."""
    cur.execute(
        f"SELECT price_eur_mwh, doc_status FROM ({rendered('stg_imbalance_price')}) AS staged"  # noqa: S608
        " WHERE control_area = %s",
        (AREA,),
    )
    rows = cur.fetchall()
    assert len(rows) == 1, rows
    return rows[0]


def test_a_price_revised_back_to_its_first_value_ends_on_that_value(cursor):
    assert deliver_price(cursor, 100.0, "A01", FIRST) == 1
    assert deliver_price(cursor, 120.0, "A01", SECOND) == 1
    # The revision wins while it is the latest, so a wrong sort order cannot pass by luck.
    assert winning_price(cursor) == (120.0, "A01")
    assert deliver_price(cursor, 100.0, "A01", THIRD) == 1
    assert winning_price(cursor) == (100.0, "A01")


def test_fetching_an_unchanged_price_again_stores_nothing(cursor):
    assert deliver_price(cursor, 100.0, "A01", FIRST) == 1
    assert deliver_price(cursor, 100.0, "A01", SECOND) == 0
    assert winning_price(cursor) == (100.0, "A01")


def test_a_final_price_beats_an_intermediate_one_received_later(cursor):
    assert deliver_price(cursor, 100.0, "A02", FIRST) == 1
    assert deliver_price(cursor, 90.0, "A01", SECOND) == 1
    assert winning_price(cursor) == (100.0, "A02")


def test_a_price_that_only_turns_final_is_recorded(cursor):
    assert deliver_price(cursor, 100.0, "A01", FIRST) == 1
    assert deliver_price(cursor, 100.0, "A02", SECOND) == 1
    assert winning_price(cursor) == (100.0, "A02")


def test_the_database_refuses_a_price_that_is_not_a_number(cursor):
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver_price(cursor, float("nan"), "A01", FIRST)


def test_a_load_value_revised_back_to_its_first_value_ends_on_that_value(cursor):
    for load, received_at in ((5400.0, FIRST), (5500.0, SECOND), (5400.0, THIRD)):
        rows = load_rows([LoadPoint(INTERVAL, load, QUARTER)], AREA, received_at)
        cursor.executemany(_INSERT_ACTUAL_LOAD, rows)
        assert cursor.rowcount == 1
    cursor.execute(
        f"SELECT load_mw FROM ({rendered('stg_actual_load')}) AS staged WHERE bidding_zone = %s",  # noqa: S608
        (AREA,),
    )
    assert cursor.fetchall() == [(5400.0,)]


def test_without_a_given_time_the_database_clock_stamps_the_row(cursor):
    point = ImbalancePricePoint(INTERVAL, "A04", 100.0, "A01", QUARTER)
    cursor.executemany(_INSERT_IMBALANCE_PRICE, imbalance_rows([point], AREA))
    cursor.execute(
        "SELECT received_at = now() FROM raw.imbalance_price WHERE control_area = %s", (AREA,)
    )
    assert cursor.fetchall() == [(True,)]
