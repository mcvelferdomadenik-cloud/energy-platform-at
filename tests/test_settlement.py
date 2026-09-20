"""The forecast and the cost per customer, proved against a real Postgres.

Every case builds its own small world in a year nothing real lives in: two customers, one hour of
profiles, prices, community totals and readings, inside a transaction that is always rolled back.
Needs the running warehouse, so it is skipped unless WAREHOUSE_DSN is set:

    uv run --env-file .env pytest tests/test_settlement.py
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

if not os.environ.get("WAREHOUSE_DSN"):
    pytest.skip("needs the running warehouse and WAREHOUSE_DSN", allow_module_level=True)

import psycopg  # noqa: E402

from dbt_sql import VARS, rendered  # noqa: E402
from megavolt.warehouse import dsn  # noqa: E402

# Invented, so a row that ever escaped the rollback would fail the staging tests on bidding zone
# and control area instead of passing for Austrian data.
ZONE = "10YAT-TEST-----X"
FIRST_CUSTOMER = "AT09999908430TESTSETTLEMENTAAAAAA"
SECOND_CUSTOMER = "AT09999908430TESTSETTLEMENTBBBBBB"
CUSTOMERS = (FIRST_CUSTOMER, SECOND_CUSTOMER)
ANNUAL_KWH = 1000.0
REGISTERED = datetime(2001, 1, 1, tzinfo=UTC)
HOUR = datetime(2001, 6, 21, 10, 0, tzinfo=UTC)
QUARTERS = [HOUR + timedelta(minutes=15 * step) for step in range(4)]
SHAPE = [0.04, 0.06, 0.08, 0.10]
SENT = datetime(2001, 6, 23, 2, 30, tzinfo=UTC)


@pytest.fixture
def cursor():
    """Two registered customers and an hour of profiles; nothing here ever reaches the warehouse."""
    # Never commits: raw is append-only for this role, so a committed test row could not be removed.
    connection = psycopg.connect(dsn(), autocommit=False)
    try:
        with connection.cursor() as cur:
            cur.executemany(
                "INSERT INTO raw.metering_point (metering_point, valid_from, profile_type, segment,"
                " annual_kwh, meter_id, payload_hash)"
                " VALUES (%s, %s, 'H0', 'household', %s, %s, %s)",
                [
                    (point, REGISTERED, ANNUAL_KWH, f"MV-{point[-1]}", f"t-{point}")
                    for point in CUSTOMERS
                ],
            )
            cur.executemany(
                "INSERT INTO raw.load_profile (profile_type, interval_start, profile_year, value,"
                " payload_hash) VALUES (%s, %s, 2001, %s, %s)",
                [("H0", q, value, f"t-h0-{q}") for q, value in zip(QUARTERS, SHAPE, strict=True)]
                + [("E1", q, 0.0, f"t-e1-{q}") for q in QUARTERS],
            )
            yield cur
    finally:
        connection.rollback()
        connection.close()


def day_ahead(cur, start: datetime, price: float, resolution: timedelta) -> None:
    cur.execute(
        "INSERT INTO raw.day_ahead_price (interval_start, bidding_zone, resolution, price_eur_mwh,"
        " payload_hash) VALUES (%s, %s, %s, %s, %s)",
        (start, ZONE, resolution, price, f"t-da-{start}"),
    )


def imbalance(cur, start: datetime, long_price: float, short_price: float) -> None:
    cur.executemany(
        "INSERT INTO raw.imbalance_price (interval_start, control_area, category, doc_status,"
        " resolution, price_eur_mwh, payload_hash)"
        " VALUES (%s, %s, %s, 'A02', '15 minutes', %s, %s)",
        [
            (start, ZONE, "A04", long_price, f"t-l-{start}"),
            (start, ZONE, "A05", short_price, f"t-s-{start}"),
        ],
    )


def took(cur, point: str, start: datetime, kwh: float) -> None:
    """A reading with no community share: what the customer took from us is what they consumed."""
    cur.execute(
        "INSERT INTO raw.meter_reading (metering_point, interval_start, consumption_kwh,"
        " allocated_kwh, meter_id, version, delivered_at, payload_hash)"
        " VALUES (%s, %s, %s, 0, %s, 1, %s, %s)",
        (point, start, kwh, f"MV-{point[-1]}", SENT, f"t-r-{point}-{start}"),
    )


def costs(cur) -> dict[tuple[str, datetime], dict]:
    """The customer cost mart for the test customers, keyed by customer and quarter hour."""
    cur.execute(
        f"SELECT * FROM ({rendered('fct_customer_cost')}) AS mart"  # noqa: S608
        " WHERE metering_point = ANY(%s) AND interval_start >= %s AND interval_start < %s"
        " ORDER BY metering_point, interval_start",
        # The test customers never leave, so they are customers in every real priced quarter hour
        # too; only the hour built here is theirs to assert on.
        (list(CUSTOMERS), HOUR, HOUR + timedelta(hours=1)),
    )
    columns = [column.name for column in cur.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    return {(row["metering_point"], row["interval_start"]): row for row in rows}


def test_the_forecast_is_annual_consumption_times_the_profile(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(hours=1))
    mart = costs(cursor)
    assert len(mart) == 8
    for quarter, value in zip(QUARTERS, SHAPE, strict=True):
        assert mart[FIRST_CUSTOMER, quarter]["forecast_residual_kwh"] == pytest.approx(
            ANNUAL_KWH / 1000 * value
        )


def test_an_hourly_auction_flattens_the_purchase_over_its_four_quarter_hours(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(hours=1))
    mart = costs(cursor)
    flat = sum(SHAPE) / 4 * ANNUAL_KWH / 1000
    assert [mart[FIRST_CUSTOMER, q]["bought_kwh"] for q in QUARTERS] == pytest.approx([flat] * 4)
    assert mart[FIRST_CUSTOMER, HOUR]["day_ahead_cost_eur"] == pytest.approx(flat / 1000 * 100.0)


def test_a_quarter_hourly_auction_buys_exactly_the_forecast(cursor):
    for quarter in QUARTERS:
        day_ahead(cursor, quarter, 100.0, timedelta(minutes=15))
    mart = costs(cursor)
    assert [mart[FIRST_CUSTOMER, q]["bought_kwh"] for q in QUARTERS] == pytest.approx(
        [ANNUAL_KWH / 1000 * value for value in SHAPE]
    )


def test_the_sun_the_community_is_expected_to_catch_lowers_what_we_buy(cursor):
    cursor.execute(
        "INSERT INTO raw.load_profile (profile_type, interval_start, profile_year, value,"
        " received_at, payload_hash) VALUES ('E1', %s, 2001, 0.05, now() + interval '1 hour',"
        " 't-e1-sunny')",
        (HOUR,),
    )
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    mart = costs(cursor)
    ours = 2 * ANNUAL_KWH / 1000 * SHAPE[0]
    expected_coverage = min(
        1.0,
        VARS["community_plant_kwp"]
        * VARS["community_yield_kwh_per_kwp"]
        / 1000
        * 0.05
        / (ours * VARS["community_annual_kwh"] / (2 * ANNUAL_KWH)),
    )
    assert 0 < expected_coverage < 1
    assert mart[FIRST_CUSTOMER, HOUR]["forecast_residual_kwh"] == pytest.approx(
        ANNUAL_KWH / 1000 * SHAPE[0] * (1 - expected_coverage)
    )


def test_a_customer_is_attributed_the_imbalance_they_cause_at_the_portfolios_price(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=50.0, short_price=250.0)
    bought = ANNUAL_KWH / 1000 * SHAPE[0]
    took(cursor, FIRST_CUSTOMER, HOUR, bought - 0.01)
    took(cursor, SECOND_CUSTOMER, HOUR, bought + 0.05)
    mart = costs(cursor)
    first, second = mart[FIRST_CUSTOMER, HOUR], mart[SECOND_CUSTOMER, HOUR]
    # Together we delivered more than we bought, so everybody settles at the short price.
    assert first["imbalance_price_eur_mwh"] == second["imbalance_price_eur_mwh"] == 250.0
    assert first["imbalance_kwh"] == pytest.approx(-0.01)
    assert first["imbalance_cost_eur"] == pytest.approx(-0.01 / 1000 * 250.0)
    assert second["imbalance_cost_eur"] == pytest.approx(0.05 / 1000 * 250.0)
    total = first["imbalance_cost_eur"] + second["imbalance_cost_eur"]
    assert total == pytest.approx((0.05 - 0.01) / 1000 * 250.0)


def test_when_the_portfolio_is_long_everybody_settles_at_the_long_price(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=50.0, short_price=250.0)
    bought = ANNUAL_KWH / 1000 * SHAPE[0]
    took(cursor, FIRST_CUSTOMER, HOUR, bought - 0.03)
    took(cursor, SECOND_CUSTOMER, HOUR, bought + 0.01)
    mart = costs(cursor)
    assert {mart[point, HOUR]["imbalance_price_eur_mwh"] for point in CUSTOMERS} == {50.0}


def test_a_reading_that_has_not_arrived_leaves_the_imbalance_unknown_never_zero(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=50.0, short_price=250.0)
    took(cursor, FIRST_CUSTOMER, HOUR, 0.05)
    mart = costs(cursor)
    missing = mart[SECOND_CUSTOMER, HOUR]
    assert missing["is_settled"] is False
    assert missing["actual_kwh"] is None and missing["imbalance_cost_eur"] is None
    assert missing["day_ahead_cost_eur"] == pytest.approx(
        ANNUAL_KWH / 1000 * SHAPE[0] / 1000 * 100.0
    )
    assert mart[FIRST_CUSTOMER, HOUR]["is_settled"] is True


def test_what_is_attributed_to_customers_is_what_the_portfolio_cost(cursor):
    day_ahead(cursor, HOUR, 80.0, timedelta(hours=1))
    for quarter in QUARTERS:
        imbalance(cursor, quarter, long_price=40.0, short_price=120.0)
        took(cursor, FIRST_CUSTOMER, quarter, 0.07)
        took(cursor, SECOND_CUSTOMER, quarter, 0.11)
    cursor.execute(f"SELECT count(*) FROM ({rendered('customer_cost_reconciles')}) AS broken")  # noqa: S608
    assert cursor.fetchone() == (0,)
    mart = costs(cursor)
    delivered = 4 * (0.07 + 0.11)
    bought = sum(row["bought_kwh"] for row in mart.values())
    assert bought == pytest.approx(2 * sum(SHAPE) * ANNUAL_KWH / 1000)
    assert sum(row["imbalance_kwh"] for row in mart.values()) == pytest.approx(delivered - bought)


def test_a_reading_in_a_priced_quarter_hour_without_a_forecast_is_reported(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    took(cursor, FIRST_CUSTOMER, HOUR, 0.05)
    cursor.execute(
        f"SELECT count(*) FROM ({rendered('every_reading_has_a_cost')}) AS lost"  # noqa: S608
        " WHERE metering_point = ANY(%s)",
        (list(CUSTOMERS),),
    )
    assert cursor.fetchone() == (0,)
    # The same reading for a quarter hour whose profile value is missing has no forecast.
    orphan = HOUR + timedelta(days=1)
    day_ahead(cursor, orphan, 100.0, timedelta(minutes=15))
    took(cursor, FIRST_CUSTOMER, orphan, 0.05)
    cursor.execute(
        f"SELECT interval_start FROM ({rendered('every_reading_has_a_cost')}) AS lost"  # noqa: S608
        " WHERE metering_point = ANY(%s)",
        (list(CUSTOMERS),),
    )
    assert cursor.fetchall() == [(orphan,)]


def test_the_portfolio_compares_delivery_only_with_the_purchase_for_settled_customers(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=50.0, short_price=250.0)
    bought = ANNUAL_KWH / 1000 * SHAPE[0]
    took(cursor, FIRST_CUSTOMER, HOUR, bought)
    cursor.execute(
        f"SELECT * FROM ({rendered('fct_supplier_settlement')}) AS mart WHERE interval_start = %s",  # noqa: S608
        (HOUR,),
    )
    columns = [column.name for column in cursor.description]
    (row,) = [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]
    assert (row["customers_bought_for"], row["customers_settled"]) == (2, 1)
    assert row["is_complete"] is False
    # One customer took exactly what was bought for them: the settled side shows no gap at all,
    # and the purchase for the customer who has not reported stands apart.
    assert row["day_ahead_cost_eur"] == pytest.approx(row["perfect_forecast_cost_eur"])
    assert row["unsettled_day_ahead_cost_eur"] == pytest.approx(bought / 1000 * 100.0)


def revisions(cur) -> dict[str, dict]:
    """The revision mart for the test customers on the day the tests build, keyed by customer."""
    cur.execute(
        f"SELECT * FROM ({rendered('fct_settlement_revision')}) AS mart"  # noqa: S608
        " WHERE metering_point = ANY(%s) AND delivery_day = %s",
        (list(CUSTOMERS), HOUR.date()),
    )
    columns = [column.name for column in cur.description]
    return {row[0]: dict(zip(columns, row, strict=True)) for row in cur.fetchall()}


def arrived(cur, point: str, kwh: float, days_after: int, version: int = 1) -> None:
    """A reading for the test hour that arrives so many days after delivery, at 02:30."""
    when = HOUR.replace(hour=2, minute=30) + timedelta(days=days_after)
    cur.execute(
        "INSERT INTO raw.meter_reading (metering_point, interval_start, consumption_kwh,"
        " allocated_kwh, meter_id, version, delivered_at, payload_hash)"
        " VALUES (%s, %s, %s, 0, %s, %s, %s, %s)",
        (point, HOUR, kwh, f"MV-{point[-1]}", version, when, f"t-a-{point}-{days_after}-{version}"),
    )


def test_a_reading_that_came_late_explains_its_whole_cost_as_late(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=200.0, short_price=200.0)
    bought = ANNUAL_KWH / 1000 * SHAPE[0]
    arrived(cursor, FIRST_CUSTOMER, bought + 0.05, days_after=1)
    # The earliest a late reading can come: the second morning. A cutoff one day too generous
    # would take it for a first delivery.
    arrived(cursor, SECOND_CUSTOMER, bought + 0.02, days_after=2)
    mart = revisions(cursor)
    assert mart[FIRST_CUSTOMER]["reason"] == "unchanged"
    assert mart[FIRST_CUSTOMER]["revision_eur"] == 0
    late = mart[SECOND_CUSTOMER]
    assert (late["reason"], late["preliminary_imbalance_cost_eur"]) == ("late", None)
    assert late["revision_eur"] == pytest.approx(0.02 / 1000 * 200.0)


def test_a_corrected_reading_is_revised_by_exactly_the_energy_it_moved(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=200.0, short_price=200.0)
    bought = ANNUAL_KWH / 1000 * SHAPE[0]
    arrived(cursor, FIRST_CUSTOMER, bought + 0.05, days_after=1)
    arrived(cursor, FIRST_CUSTOMER, bought + 0.01, days_after=7, version=2)
    corrected = revisions(cursor)[FIRST_CUSTOMER]
    assert corrected["reason"] == "corrected"
    assert corrected["preliminary_imbalance_cost_eur"] == pytest.approx(0.05 / 1000 * 200.0)
    assert corrected["final_imbalance_cost_eur"] == pytest.approx(0.01 / 1000 * 200.0)
    assert corrected["revision_eur"] == pytest.approx(-0.04 / 1000 * 200.0)


def test_a_customer_who_never_reported_is_missing_and_moves_nothing(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=200.0, short_price=200.0)
    arrived(cursor, FIRST_CUSTOMER, 0.05, days_after=1)
    silent = revisions(cursor)[SECOND_CUSTOMER]
    assert (silent["reason"], silent["revision_eur"]) == ("missing", 0)
    assert silent["final_imbalance_cost_eur"] is None


def test_the_same_version_sent_again_with_another_value_is_a_correction(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    imbalance(cursor, HOUR, long_price=200.0, short_price=200.0)
    arrived(cursor, FIRST_CUSTOMER, 0.09, days_after=1)
    arrived(cursor, FIRST_CUSTOMER, 0.07, days_after=5)
    resent = revisions(cursor)[FIRST_CUSTOMER]
    assert resent["reason"] == "corrected"
    assert resent["revision_eur"] == pytest.approx(-0.02 / 1000 * 200.0)


def test_a_reading_without_an_imbalance_price_is_counted_not_summed_away(cursor):
    day_ahead(cursor, HOUR, 100.0, timedelta(minutes=15))
    arrived(cursor, FIRST_CUSTOMER, 0.09, days_after=1)
    unpriced = revisions(cursor)[FIRST_CUSTOMER]
    assert unpriced["unpriced_quarter_hours"] == 1
    assert unpriced["final_imbalance_cost_eur"] is None
