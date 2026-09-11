"""Store the Austrian day-ahead prices for the next delivery day, once a day."""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import dag, task

from megavolt.entsoe import AT_BIDDING_ZONE, day_ahead_prices
from megavolt.warehouse import store_day_ahead_prices

VIENNA = "Europe/Vienna"
RESOLUTION = timedelta(minutes=15)


@dag(
    dag_id="entsoe_day_ahead_prices",
    schedule="0 14 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 3, "retry_delay": timedelta(minutes=15)},
    tags=["entsoe", "prices"],
)
def entsoe_day_ahead_prices():
    """The day-ahead auction for delivery day D publishes around 13:00 on D-1."""

    @task
    def fetch_and_store(**context) -> int:
        """Fetch the next delivery day and write it to raw.day_ahead_price."""
        run_day = context["logical_date"].in_timezone(VIENNA).start_of("day")
        delivery_day = run_day.add(days=1)

        prices = day_ahead_prices(delivery_day, delivery_day.add(days=1))
        stored = store_day_ahead_prices(prices, AT_BIDDING_ZONE, RESOLUTION)
        print(f"{delivery_day:%Y-%m-%d}: fetched {len(prices)}, stored {stored} new rows")
        return stored

    fetch_and_store()


entsoe_day_ahead_prices()
