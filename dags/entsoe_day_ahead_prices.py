"""Store the Austrian day-ahead prices for the next delivery day, once a day.

Missed afternoons are caught up in order (`catchup=True`): the auction results stay available, a
day the server slept through is fetched when it is back, and a day already stored stores nothing.

A day ENTSO-E has not published yet is waited for, not failed: the task is a sensor that asks
again every half hour and stores the day the moment it is there. Any other error (token, parsing,
network) still fails after the usual retries, and a day still unpublished when the patience runs
out ends failed, so the night shift reports it instead of the day passing empty.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import dag, task

from megavolt.entsoe import AT_BIDDING_ZONE, EntsoeError, day_ahead_prices
from megavolt.warehouse import store_day_ahead_prices

VIENNA = "Europe/Vienna"
NOT_PUBLISHED = "No matching data"


@dag(
    dag_id="entsoe_day_ahead_prices",
    schedule="0 14 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=True,
    max_active_runs=1,
    default_args={"retries": 3, "retry_delay": timedelta(minutes=15)},
    tags=["entsoe", "prices"],
)
def entsoe_day_ahead_prices():
    """The day-ahead auction for delivery day D publishes around 13:00 on D-1."""

    # The patience runs from the task's start: an on-time 14:00 run gives up at 10:00 the next
    # morning, before the next day's run, so it does not hold up the days behind it.
    @task.sensor(
        poke_interval=timedelta(minutes=30), timeout=timedelta(hours=20), mode="reschedule"
    )
    def fetch_and_store(**context) -> bool:
        """Store the next delivery day in raw.day_ahead_price, or ask again if it is not out."""
        # A manual run has no logical_date in Airflow 3, only run_after, which is a plain datetime.
        moment = pendulum.instance(context.get("logical_date") or context["dag_run"].run_after)
        run_day = moment.in_timezone(VIENNA).start_of("day")
        delivery_day = run_day.add(days=1)

        try:
            prices = day_ahead_prices(delivery_day, delivery_day.add(days=1))
        except EntsoeError as exc:
            if NOT_PUBLISHED not in str(exc):
                raise
            print(f"{delivery_day:%Y-%m-%d}: not published yet ({exc})")
            return False
        stored = store_day_ahead_prices(prices, AT_BIDDING_ZONE)
        print(f"{delivery_day:%Y-%m-%d}: fetched {len(prices)}, stored {stored} new rows")
        return True

    fetch_and_store()


entsoe_day_ahead_prices()
