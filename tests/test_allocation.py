"""The allocation and supplier position marts, proved against a real Postgres.

Every case gets hand-written rows in a year nothing real lives in, inside a transaction that is
always rolled back. Needs the running warehouse, so it is skipped unless WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_allocation.py
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import rendered  # noqa: E402
from megavolt.warehouse import dsn  # noqa: E402

FIRST_POINT = "AT09999908430TESTALLOCATIONAAAAAA"
SECOND_POINT = "AT09999908430TESTALLOCATIONBBBBBB"
REGISTERED = datetime(2001, 1, 1, tzinfo=UTC)
NOON = datetime(2001, 6, 21, 10, 0, tzinfo=UTC)
NIGHT = NOON + timedelta(hours=12)
SUNNY = NOON + timedelta(days=1)
DELIVERED = datetime(2001, 6, 23, 2, 30, tzinfo=UTC)


@pytest.fixture
def cursor():
    """A cursor with two registered test customers, whose work never reaches the warehouse."""
    # Never commits: raw is append-only for this role, so a committed test row could not be removed.
    connection = psycopg.connect(dsn(), autocommit=False)
    try:
        with connection.cursor() as cur:
            cur.executemany(
                "INSERT INTO raw.metering_point (metering_point, valid_from, profile_type, segment,"
                " annual_kwh, meter_id, payload_hash)"
                " VALUES (%s, %s, 'H0', 'household', 3500, %s, %s)",
                [
                    (FIRST_POINT, REGISTERED, "MV-TEST-A", "test-allocation-a"),
                    (SECOND_POINT, REGISTERED, "MV-TEST-B", "test-allocation-b"),
                ],
            )
            yield cur
    finally:
        connection.rollback()
        connection.close()


def community(cur, moment: datetime, generation: float, consumption: float) -> None:
    """What the community reported about itself for one quarter hour."""
    cur.execute(
        "INSERT INTO raw.community_interval (interval_start, generation_kwh, consumption_kwh,"
        " version, delivered_at, payload_hash) VALUES (%s, %s, %s, 1, %s, %s)",
        (moment, generation, consumption, DELIVERED, f"test-community-{moment.isoformat()}"),
    )


def reading(cur, point: str, moment: datetime, consumption: float, allocated: float, version=1):
    """One delivered reading for a test customer."""
    meter = "MV-TEST-A" if point == FIRST_POINT else "MV-TEST-B"
    cur.execute(
        "INSERT INTO raw.meter_reading (metering_point, interval_start, consumption_kwh,"
        " allocated_kwh, meter_id, version, delivered_at, payload_hash)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            point,
            moment,
            consumption,
            allocated,
            meter,
            version,
            DELIVERED + timedelta(days=version),
            f"test-reading-{point[-1]}-{moment.isoformat()}-{version}",
        ),
    )


def allocation(cur, moment: datetime) -> list[dict]:
    """The allocation mart's rows for the test customers at one quarter hour."""
    cur.execute(
        f"SELECT * FROM ({rendered('fct_community_allocation')}) AS mart"  # noqa: S608
        " WHERE interval_start = %s AND metering_point = ANY(%s) ORDER BY metering_point",
        (moment, [FIRST_POINT, SECOND_POINT]),
    )
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def position(cur, moment: datetime) -> dict:
    """The supplier position mart's row for one quarter hour."""
    cur.execute(
        f"SELECT * FROM ({rendered('fct_supplier_position')}) AS mart WHERE interval_start = %s",  # noqa: S608
        (moment,),
    )
    columns = [column.name for column in cur.description]
    (row,) = cur.fetchall()
    return dict(zip(columns, row, strict=True))


def test_the_community_covers_a_share_and_we_supply_the_rest(cursor):
    community(cursor, NOON, generation=40.0, consumption=100.0)
    reading(cursor, FIRST_POINT, NOON, consumption=2.0, allocated=0.8)
    (row,) = allocation(cursor, NOON)
    assert row["coverage_ratio"] == pytest.approx(0.4)
    assert row["residual_kwh"] == pytest.approx(1.2)
    assert row["expected_allocated_kwh"] == pytest.approx(0.8)
    assert row["follows_allocation_rule"] is True


def test_at_night_the_community_covers_nothing_and_we_supply_everything(cursor):
    community(cursor, NIGHT, generation=0.0, consumption=80.0)
    reading(cursor, FIRST_POINT, NIGHT, consumption=1.5, allocated=0.0)
    (row,) = allocation(cursor, NIGHT)
    assert (row["coverage_ratio"], row["residual_kwh"]) == (0.0, 1.5)
    assert row["follows_allocation_rule"] is True


def test_when_the_sun_outruns_the_community_nobody_gets_more_than_they_consumed(cursor):
    community(cursor, SUNNY, generation=150.0, consumption=100.0)
    reading(cursor, FIRST_POINT, SUNNY, consumption=2.0, allocated=2.0)
    (row,) = allocation(cursor, SUNNY)
    assert (row["coverage_ratio"], row["residual_kwh"]) == (1.0, 0.0)
    spot = position(cursor, SUNNY)
    assert spot["community_allocated_kwh"] == 100.0
    assert spot["community_surplus_kwh"] == 50.0
    assert spot["our_residual_kwh"] == 0.0


def test_a_wrong_first_delivery_breaks_the_rule_and_its_correction_restores_it(cursor):
    community(cursor, NOON, generation=40.0, consumption=100.0)
    reading(cursor, FIRST_POINT, NOON, consumption=3.0, allocated=0.8)
    (row,) = allocation(cursor, NOON)
    assert row["follows_allocation_rule"] is False
    reading(cursor, FIRST_POINT, NOON, consumption=2.0, allocated=0.8, version=2)
    (row,) = allocation(cursor, NOON)
    assert (row["version"], row["follows_allocation_rule"]) == (2, True)


def test_a_reading_without_community_totals_is_kept_and_looks_unknown(cursor):
    reading(cursor, FIRST_POINT, NOON, consumption=2.0, allocated=0.8)
    (row,) = allocation(cursor, NOON)
    assert row["residual_kwh"] == pytest.approx(1.2)
    assert row["coverage_ratio"] is None and row["follows_allocation_rule"] is None


def test_our_position_is_the_sum_of_our_customers_and_knows_who_is_missing(cursor):
    community(cursor, NOON, generation=40.0, consumption=100.0)
    reading(cursor, FIRST_POINT, NOON, consumption=2.0, allocated=0.8)
    reading(cursor, SECOND_POINT, NOON, consumption=1.0, allocated=0.4)
    spot = position(cursor, NOON)
    assert spot["customers_reporting"] == 2
    assert spot["our_consumption_kwh"] == pytest.approx(3.0)
    assert spot["our_allocated_kwh"] == pytest.approx(1.2)
    assert spot["our_residual_kwh"] == pytest.approx(1.8)
    assert spot["community_allocated_kwh"] + spot["community_surplus_kwh"] == 40.0
    # Registered AT that quarter hour: in 2001 that is the two test customers and nobody else.
    assert (spot["customers_registered"], spot["is_complete"]) == (2, True)


def test_a_quarter_hour_is_incomplete_while_a_registered_customer_has_not_reported(cursor):
    community(cursor, NOON, generation=40.0, consumption=100.0)
    reading(cursor, FIRST_POINT, NOON, consumption=2.0, allocated=0.8)
    spot = position(cursor, NOON)
    assert (spot["customers_registered"], spot["customers_reporting"]) == (2, 1)
    assert spot["is_complete"] is False


def test_a_wrong_reading_at_night_still_follows_the_rule_so_the_flag_is_no_proof(cursor):
    community(cursor, NIGHT, generation=0.0, consumption=80.0)
    reading(cursor, FIRST_POINT, NIGHT, consumption=9.9, allocated=0.0)
    (row,) = allocation(cursor, NIGHT)
    assert row["follows_allocation_rule"] is True


def test_a_community_that_consumes_nothing_gives_a_defined_coverage(cursor):
    community(cursor, NIGHT, generation=5.0, consumption=0.0)
    reading(cursor, FIRST_POINT, NIGHT, consumption=0.0, allocated=0.0)
    (row,) = allocation(cursor, NIGHT)
    assert (row["coverage_ratio"], row["follows_allocation_rule"]) == (1, True)


def test_the_database_refuses_a_reading_or_a_total_that_is_not_a_number(cursor):
    for consumption in (float("nan"), float("inf")):
        with pytest.raises(psycopg.errors.CheckViolation):
            reading(cursor, FIRST_POINT, NOON, consumption=consumption, allocated=0.0)
        cursor.connection.rollback()
    for generation, consumption in ((float("nan"), 1.0), (1.0, float("inf"))):
        with pytest.raises(psycopg.errors.CheckViolation):
            community(cursor, NOON, generation=generation, consumption=consumption)
        cursor.connection.rollback()
