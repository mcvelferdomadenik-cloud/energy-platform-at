"""The winner rule in the dbt model stg_meter_reading, proved against a real Postgres.

Every case gets its own hand-written rows, because the simulator's data only exercises some of them
by chance. The rows are inserted inside a transaction that is always rolled back, so raw is never
changed. Needs the running warehouse, so it is skipped unless WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_stg_meter_reading.py
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import rendered  # noqa: E402
from megavolt.warehouse import dsn  # noqa: E402

QUERY = rendered("stg_meter_reading")

POINT = "AT09999908430TESTTESTTESTTESTTEST"
STRANGER = "AT09999908430STRANGERSTRANGERSTR"
DAY = datetime(2025, 3, 1, tzinfo=UTC)
SWAP = DAY + timedelta(hours=1)


def at(minutes: int) -> datetime:
    """An interval start, in minutes after the fixture day begins."""
    return DAY + timedelta(minutes=minutes)


FIRST = datetime(2025, 3, 2, 2, 30, tzinfo=UTC)
LATER = datetime(2025, 3, 3, 2, 30, tzinfo=UTC)
CORRECTION = datetime(2025, 3, 9, 2, 30, tzinfo=UTC)

REGISTRY = [
    (POINT, DAY, "H0", "household", 3500.0, "MV-900001-A", "test-registry-a"),
    (POINT, SWAP, "H0", "household", 3500.0, "MV-900001-B", "test-registry-b"),
]

# (metering_point, interval_start, consumption, meter_id, version, delivered_at, payload_hash)
READINGS = [
    # 00:00 corrected: version 2 must beat version 1.
    (POINT, at(0), 1.0, "MV-900001-A", 1, FIRST, "test-00-v1"),
    (POINT, at(0), 2.0, "MV-900001-A", 2, CORRECTION, "test-00-v2"),
    # 00:15 untouched by the correction: version 1 must survive.
    (POINT, at(15), 1.1, "MV-900001-A", 1, FIRST, "test-15-v1"),
    # 00:30 re-sent at the same version: the later delivery must win.
    (POINT, at(30), 1.2, "MV-900001-A", 1, FIRST, "test-30-first"),
    (POINT, at(30), 1.3, "MV-900001-A", 1, LATER, "test-30-later"),
    # 00:45 same version, same delivery, different value: payload_hash must decide, every time.
    (POINT, at(45), 1.5, "MV-900001-A", 1, FIRST, "test-45-a"),
    (POINT, at(45), 1.6, "MV-900001-A", 1, FIRST, "test-45-b"),
    # 01:00 after the meter swap, sent by the new meter.
    (POINT, at(60), 1.4, "MV-900001-B", 1, FIRST, "test-60-v1"),
    # 01:15 after the swap, but sent by the old meter: kept, and flagged.
    (POINT, at(75), 1.7, "MV-900001-A", 1, FIRST, "test-75-v1"),
    # A point that was never registered as ours.
    (STRANGER, at(0), 9.9, "MV-999999-A", 1, FIRST, "test-stranger"),
]


@pytest.fixture(scope="module")
def staged():
    """Run the staging query over the fixture rows and roll everything back afterwards."""
    # Never commits: raw is append-only for this role, so a committed test row could not be removed.
    connection = psycopg.connect(dsn(), autocommit=False)
    try:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO raw.metering_point (metering_point, valid_from, profile_type, segment,"
                " annual_kwh, meter_id, payload_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                REGISTRY,
            )
            cursor.executemany(
                "INSERT INTO raw.meter_reading (metering_point, interval_start, consumption_kwh,"
                " allocated_kwh, meter_id, version, delivered_at, payload_hash)"
                " VALUES (%s, %s, %s, 0.0, %s, %s, %s, %s)",
                READINGS,
            )
            runs = []
            for _ in range(2):
                cursor.execute(QUERY)
                columns = [column.name for column in cursor.description]
                runs.append(
                    [
                        dict(zip(columns, row, strict=True))
                        for row in cursor.fetchall()
                        if row[0] in (POINT, STRANGER)
                    ]
                )
        yield runs
    finally:
        connection.rollback()
        connection.close()


def row(staged, minutes):
    """The single staged row for the test point at one interval."""
    matches = [
        r for r in staged[0] if r["metering_point"] == POINT and r["interval_start"] == at(minutes)
    ]
    assert len(matches) == 1, f"expected exactly one row at {minutes} min, got {len(matches)}"
    return matches[0]


def test_each_interval_appears_exactly_once(staged):
    point_rows = [r for r in staged[0] if r["metering_point"] == POINT]
    assert len(point_rows) == len({r["interval_start"] for r in point_rows}) == 6


def test_a_correction_beats_the_delivery_it_corrects(staged):
    assert row(staged, 0)["version"] == 2
    assert row(staged, 0)["consumption_kwh"] == 2.0


def test_an_interval_the_correction_left_out_keeps_version_one(staged):
    assert row(staged, 15)["version"] == 1
    assert row(staged, 15)["consumption_kwh"] == 1.1


def test_the_later_delivery_wins_when_the_version_is_the_same(staged):
    assert row(staged, 30)["consumption_kwh"] == 1.3


def test_an_exact_tie_is_broken_by_payload_hash_and_breaks_the_same_way_every_run(staged):
    assert row(staged, 45)["consumption_kwh"] == 1.6
    assert staged[0] == staged[1]


def test_a_reading_after_a_meter_swap_is_matched_to_the_new_meter(staged):
    assert row(staged, 60)["registered_meter_id"] == "MV-900001-B"
    assert row(staged, 60)["meter_matches_registry"] is True


def test_a_reading_before_the_swap_is_matched_to_the_old_meter(staged):
    assert row(staged, 0)["registered_meter_id"] == "MV-900001-A"


def test_a_reading_from_the_wrong_meter_is_kept_and_flagged(staged):
    assert row(staged, 75)["registered_meter_id"] == "MV-900001-B"
    assert row(staged, 75)["meter_matches_registry"] is False


def test_a_point_that_is_not_ours_never_reaches_staging(staged):
    assert not [r for r in staged[0] if r["metering_point"] == STRANGER]


def test_the_rows_carry_the_registered_profile(staged):
    assert {r["profile_type"] for r in staged[0]} == {"H0"}
