"""Store hourly temperature and global radiation where the community's plant stands, once a day.

Runs before the meter stream, which generates from yesterday's radiation; the analysis is about an
hour behind the clock, so yesterday is complete by then.

Source: GeoSphere Austria, INCA analysis, CC BY 4.0. The whole window is one request, so a backfill
is this same DAG with a larger `window_days`: a year still costs one of the 240 requests the data
hub allows an hour. The analysis can be reprocessed, so the last days are fetched again every run
and the warehouse keeps only what changed.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import Param, dag, task

from megavolt.community import LATITUDE, LONGITUDE
from megavolt.geosphere import DATASET, GeosphereError, in_range, weather
from megavolt.warehouse import store_weather

# A technical choice: long enough to pick up a reprocessed analysis, short enough to stay small.
WINDOW_DAYS = 10


@dag(
    dag_id="geosphere_weather",
    schedule="30 4 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz="Europe/Vienna"),
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 3,
        "retry_delay": timedelta(minutes=15),
        "execution_timeout": timedelta(minutes=30),
    },
    params={"window_days": Param(WINDOW_DAYS, type="integer", minimum=1, maximum=800)},
    tags=["geosphere", "weather"],
)
def geosphere_weather():
    """One request per run, whatever the window."""

    @task
    def fetch_and_store(**context) -> int:
        """Fetch the window that ends at the run's hour and store what changed."""
        # A manual run has no logical_date in Airflow 3, only run_after, which is a plain datetime.
        moment = pendulum.instance(context.get("logical_date") or context["dag_run"].run_after)
        end = moment.in_timezone("UTC").start_of("hour")
        # From local midnight, so the oldest day of a backfill is never stored half and frozen so.
        start = end.subtract(days=int(context["params"]["window_days"]))
        start = start.in_timezone("Europe/Vienna").start_of("day").in_timezone("UTC")
        points, odd = in_range(weather(start, end, LATITUDE, LONGITUDE))
        stored = store_weather(points, DATASET, LATITUDE, LONGITUDE)
        print(f"{start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}: {len(points)} values, {stored} new")
        # After storing, so one strange hour is loud without blocking the good ones.
        if odd:
            raise GeosphereError(f"{len(odd)} values outside their range, not stored: {odd[:5]}")
        return stored

    fetch_and_store()


geosphere_weather()
