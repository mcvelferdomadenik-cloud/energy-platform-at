"""Store Austrian imbalance prices and actual load for the last days, once a day.

Neither is final when it first appears. Imbalance prices are published a quarter hour after
delivery as intermediate (A01) and confirmed as final (A02) days later; load values get revised
too. So every run re-fetches a window of past days, one request per day so each day keeps its own
document status, and the warehouse keeps only what changed: `payload_hash` covers value and status,
so an unchanged interval inserts nothing.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pendulum
from airflow.sdk import Param, dag, task

from megavolt.entsoe import AT_BIDDING_ZONE, EntsoeError, actual_load, imbalance_prices
from megavolt.warehouse import store_actual_load, store_imbalance_prices

VIENNA = "Europe/Vienna"
# Probed on 2026-09-19: 1 August was still intermediate after 49 days, 15 July final after 66.
# The window has to reach past that, or a day would never be seen turning final.
WINDOW_DAYS = 75
# The token allows 400 requests a minute. Two tasks run side by side, so half a second each caps
# them at 240 a minute together, whatever the network does.
PAUSE_SECONDS = 0.5


def past_days(context) -> list[pendulum.DateTime]:
    """The full Austrian days to re-fetch, oldest first, so a late newest day fails last."""
    # A manual run has no logical_date in Airflow 3, only run_after, which is a plain datetime.
    moment = pendulum.instance(context.get("logical_date") or context["dag_run"].run_after)
    run_day = moment.in_timezone(VIENNA).start_of("day")
    window = int(context["params"]["window_days"])
    return [run_day.subtract(days=offset) for offset in range(window, 0, -1)]


def finished(stored: int, failed: list[str]) -> int:
    """One bad day must not hide the others, and must not pass quietly either."""
    if failed:
        raise EntsoeError(f"stored {stored} rows, but {len(failed)} days failed: {failed}")
    return stored


@dag(
    dag_id="entsoe_actuals",
    schedule="0 6 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(minutes=15),
        # A response that drips in slowly never trips the per-chunk read timeout.
        "execution_timeout": timedelta(hours=1),
    },
    params={"window_days": Param(WINDOW_DAYS, type="integer", minimum=1, maximum=400)},
    tags=["entsoe", "prices", "load"],
)
def entsoe_actuals():
    """Two independent fetches; one failing must not stop the other."""

    @task
    def fetch_imbalance_prices(**context) -> int:
        """One request per day, so each day carries its own intermediate/final status."""
        stored, failed = 0, []
        for day in past_days(context):
            try:
                points = imbalance_prices(day, day.add(days=1))
            except EntsoeError as exc:
                failed.append(f"{day:%Y-%m-%d}: {exc}")
                continue
            finally:
                time.sleep(PAUSE_SECONDS)
            new = store_imbalance_prices(points, AT_BIDDING_ZONE)
            statuses = sorted({point.doc_status for point in points})
            print(f"{day:%Y-%m-%d}: {len(points)} prices, status {statuses}, {new} new rows")
            stored += new
        return finished(stored, failed)

    @task
    def fetch_actual_load(**context) -> int:
        """Realised load per quarter hour, in MW."""
        stored, failed = 0, []
        for day in past_days(context):
            try:
                points = actual_load(day, day.add(days=1))
            except EntsoeError as exc:
                failed.append(f"{day:%Y-%m-%d}: {exc}")
                continue
            finally:
                time.sleep(PAUSE_SECONDS)
            new = store_actual_load(points, AT_BIDDING_ZONE)
            print(f"{day:%Y-%m-%d}: {len(points)} load values, {new} new rows")
            stored += new
        return finished(stored, failed)

    fetch_imbalance_prices()
    fetch_actual_load()


entsoe_actuals()
