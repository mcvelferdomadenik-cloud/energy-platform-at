"""The weather writer and its staging model, proved against a real Postgres.

Every case runs inside a transaction that is always rolled back. Needs the running warehouse, so it
is skipped unless WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_weather_revisions.py
"""

import os
from datetime import UTC, datetime

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import rendered  # noqa: E402
from megavolt.geosphere import WeatherPoint  # noqa: E402
from megavolt.warehouse import _INSERT_WEATHER, dsn, weather_rows  # noqa: E402

DATASET = "test-dataset-not-real"
PLACE = (12.34, 56.78)
HOUR = datetime(2025, 3, 30, 11, 0, tzinfo=UTC)
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


def deliver(cur, parameter: str, value: float, received_at: datetime) -> int:
    """Deliver one value and return how many rows that stored."""
    rows = weather_rows([WeatherPoint(HOUR, parameter, value)], DATASET, *PLACE, received_at)
    cur.executemany(_INSERT_WEATHER, rows)
    return cur.rowcount


def staged(cur) -> list[tuple]:
    """What the staging model says about the test location."""
    cur.execute(
        "SELECT valid_at, temperature_c, global_radiation_w_m2"  # noqa: S608
        f" FROM ({rendered('stg_weather_hourly')}) AS staged WHERE dataset = %s",
        (DATASET,),
    )
    return cur.fetchall()


def test_both_parameters_of_an_hour_land_in_one_row(cursor):
    assert deliver(cursor, "T2M", 13.4, FIRST) == 1
    assert deliver(cursor, "GL", 610.0, FIRST) == 1
    assert staged(cursor) == [(HOUR, 13.4, 610.0)]


def test_an_hour_with_no_radiation_delivered_shows_a_null_not_a_zero(cursor):
    assert deliver(cursor, "T2M", 13.4, FIRST) == 1
    assert staged(cursor) == [(HOUR, 13.4, None)]


def test_a_value_revised_back_to_its_first_reading_ends_on_that_reading(cursor):
    assert deliver(cursor, "T2M", 13.4, FIRST) == 1
    assert deliver(cursor, "T2M", 13.9, SECOND) == 1
    # The revision wins while it is the latest, so a wrong sort order cannot pass by luck.
    assert staged(cursor) == [(HOUR, 13.9, None)]
    assert deliver(cursor, "T2M", 13.4, THIRD) == 1
    assert staged(cursor) == [(HOUR, 13.4, None)]


def test_fetching_an_unchanged_value_again_stores_nothing(cursor):
    assert deliver(cursor, "T2M", 13.4, FIRST) == 1
    assert deliver(cursor, "T2M", 13.4, SECOND) == 0


def test_the_database_refuses_an_unknown_parameter_and_a_value_that_is_not_a_number(cursor):
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, "RR", 1.0, FIRST)
    cursor.connection.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, "T2M", float("nan"), FIRST)
    cursor.connection.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, "GL", -50.0, FIRST)
    cursor.connection.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, "T2M", 50.1, FIRST)
    cursor.connection.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        deliver(cursor, "GL", 1400.1, FIRST)
