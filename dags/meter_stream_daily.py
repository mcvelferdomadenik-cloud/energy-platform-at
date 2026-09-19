"""Deliver one day of meter readings to Redpanda every morning, the way EDA does.

The grid operator's overnight delivery lands at 03:30 Vienna time; this runs at 05:00, after it.
It calls the same function as `python -m megavolt.simulate`, so a scheduled run and a hand-run day
produce identical messages. The consumer service picks them up from the topic.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import dag, task

from megavolt.simulate import deliver

VIENNA = "Europe/Vienna"


@dag(
    dag_id="meter_stream_daily",
    schedule="0 5 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["meter_stream", "simulator"],
)
def meter_stream_daily():
    """A bare cron string in Airflow 3 triggers at the cron time, so logical_date is the run day."""

    @task
    def produce_delivery_day(**context) -> int:
        """Register our metering points and produce everything that arrives on the run day."""
        # A manual run has no logical_date in Airflow 3, only run_after, which is a plain datetime.
        moment = pendulum.instance(context.get("logical_date") or context["dag_run"].run_after)
        delivery_date = moment.in_timezone(VIENNA).date()
        registered, produced = deliver(delivery_date)
        print(f"{delivery_date}: registered {registered} new rows, produced {produced} messages")
        return produced

    produce_delivery_day()


meter_stream_daily()
