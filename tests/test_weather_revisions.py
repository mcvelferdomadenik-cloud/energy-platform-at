"""The weather writer and its staging model, proved against a real Postgres.

Every case runs inside a transaction that is always rolled back. Needs the running warehouse, so it
is skipped unless WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_weather_revisions.py
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import rendered  # noqa: E402
from megavolt.geosphere import WeatherPoint  # noqa: E402
from megavolt.warehouse import (  # noqa: E402
    _INSERT_WEATHER,
    _SELECT_FIRST_WEATHER,
    dsn,
    weather_rows,
)

DATASET = "test-dataset-not-real"
PLACE = (12.34, 56.78)
HOUR = datetime(2025, 3, 30, 11, 0, tzinfo=UTC)
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


# --- what the simulator may see: a day frozen when it was first complete ----------------------

NOON, EVENING = HOUR, datetime(2025, 3, 30, 18, 0, tzinfo=UTC)
# Local midnight at the end of 30 March: the last hour of that day, not the first of the next.
MIDNIGHT = datetime(2025, 3, 30, 22, 0, tzinfo=UTC)
NEXT_DAY = datetime(2025, 3, 31, 9, 0, tzinfo=UTC)


def deliver_hour(cur, valid_at: datetime, value: float, received_at: datetime) -> None:
    """Deliver one radiation value for one hour."""
    rows = weather_rows([WeatherPoint(valid_at, "GL", value)], DATASET, *PLACE, received_at)
    cur.executemany(_INSERT_WEATHER, rows)


def seen_by_the_simulator(cur) -> dict[datetime, float]:
    """The radiation of the test day as the simulator would be given it."""
    cur.execute(
        _SELECT_FIRST_WEATHER,
        (DATASET, ["GL"], *PLACE, datetime(2025, 3, 30, tzinfo=UTC), MIDNIGHT),
    )
    return {valid_at: value for _, valid_at, value in cur.fetchall()}


def test_a_day_nothing_has_arrived_after_is_not_shown_at_all(cursor):
    deliver_hour(cursor, NOON, 600.0, FIRST)
    assert seen_by_the_simulator(cursor) == {}


def test_a_complete_day_shows_its_first_values_and_ignores_a_revision(cursor):
    deliver_hour(cursor, NOON, 600.0, FIRST)
    deliver_hour(cursor, NEXT_DAY, 300.0, FIRST)
    deliver_hour(cursor, NOON, 650.0, SECOND)
    assert seen_by_the_simulator(cursor) == {NOON: 600.0}


def test_an_hour_that_turns_up_after_its_day_was_complete_stays_a_hole(cursor):
    deliver_hour(cursor, NOON, 600.0, FIRST)
    deliver_hour(cursor, NEXT_DAY, 300.0, FIRST)
    before = seen_by_the_simulator(cursor)
    deliver_hour(cursor, EVENING, 40.0, SECOND)
    # A day further on arriving later must not move the freeze point either.
    deliver_hour(cursor, NEXT_DAY + timedelta(days=1), 300.0, THIRD)
    assert seen_by_the_simulator(cursor) == before == {NOON: 600.0}


def test_the_rest_of_a_day_arriving_with_the_next_days_first_hours_is_shown_whole(cursor):
    # The live order every morning: the early hours came yesterday, the rest come today.
    deliver_hour(cursor, NOON, 600.0, FIRST)
    deliver_hour(cursor, EVENING, 40.0, SECOND)
    deliver_hour(cursor, NEXT_DAY, 300.0, SECOND)
    assert seen_by_the_simulator(cursor) == {NOON: 600.0, EVENING: 40.0}


def test_the_midnight_hour_belongs_to_the_day_it_ends(cursor):
    deliver_hour(cursor, NOON, 600.0, FIRST)
    deliver_hour(cursor, MIDNIGHT, 0.0, SECOND)
    deliver_hour(cursor, NEXT_DAY, 300.0, SECOND)
    assert seen_by_the_simulator(cursor) == {NOON: 600.0, MIDNIGHT: 0.0}


def test_a_day_is_frozen_on_all_its_hours_whatever_window_is_asked_for(cursor):
    # The first hour of the window used to be judged alone; a hole there filled later showed up.
    deliver_hour(cursor, NOON, 600.0, FIRST)
    deliver_hour(cursor, NEXT_DAY, 300.0, FIRST)
    deliver_hour(cursor, EVENING, 40.0, SECOND)
    cursor.execute(_SELECT_FIRST_WEATHER, (DATASET, ["GL"], *PLACE, EVENING, EVENING))
    assert cursor.fetchall() == []


def test_a_day_backfilled_after_newer_days_were_there_is_shown_and_then_frozen(cursor):
    deliver_hour(cursor, NEXT_DAY, 300.0, FIRST)
    deliver_hour(cursor, NOON, 600.0, SECOND)
    assert seen_by_the_simulator(cursor) == {NOON: 600.0}
    deliver_hour(cursor, EVENING, 40.0, THIRD)
    assert seen_by_the_simulator(cursor) == {NOON: 600.0}


def test_another_dataset_at_the_same_place_is_never_mixed_in(cursor):
    deliver_hour(cursor, NOON, 600.0, SECOND)
    deliver_hour(cursor, NEXT_DAY, 300.0, SECOND)
    rows = weather_rows([WeatherPoint(NOON, "GL", 111.0)], "another-test-dataset", *PLACE, FIRST)
    cursor.executemany(_INSERT_WEATHER, rows)
    assert seen_by_the_simulator(cursor) == {NOON: 600.0}


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
