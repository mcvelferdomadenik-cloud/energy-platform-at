"""Deliver one day of meter readings to Redpanda every morning, the way EDA does.

The grid operator's overnight delivery lands at 03:30 Vienna time; this runs at 05:00, after it.
It calls the same function as `python -m megavolt.simulate`, so a scheduled run and a hand-run day
produce identical messages. The consumer service picks them up from the topic.

Missed mornings are caught up in order (`catchup=True`): a day the server slept through is
delivered the moment it is back; a day already delivered produces the same messages again and the
warehouse stores nothing new. Each delivery first waits until the warehouse has frozen the weather
of the day it covers: generation is simulated from measured radiation, so a day cannot be
delivered before its weather is in, and waiting beats failing after two quick retries.
"""

from __future__ import annotations

from datetime import date, timedelta

import pendulum
from airflow.sdk import Asset, dag, task

from megavolt.consumer import backlog
from megavolt.simulate import deliver, weather_ready

# Marked once the consumer has stored the day; dbt_build is scheduled on it, not on the clock.
METER_DELIVERY = Asset("meter_delivery")

VIENNA = "Europe/Vienna"
# A timed-out sensor is not retried, and a day it gives up on is delivered only by hand (clear the
# run), so the sensor outlasts any weather outage we would not notice: a rescheduled sensor holds
# no worker, and the days queue up in order meanwhile (`max_active_runs=1`), which is right, since
# none of them could be simulated without its weather anyway.
WEATHER_POKE = timedelta(minutes=10)
WEATHER_PATIENCE = timedelta(days=7)


@dag(
    dag_id="meter_stream_daily",
    schedule="0 5 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=True,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    tags=["meter_stream", "simulator"],
)
def meter_stream_daily():
    """A bare cron string in Airflow 3 triggers at the cron time, so logical_date is the run day."""

    def delivery_date_of(context) -> date:
        # A manual run has no logical_date in Airflow 3, only run_after, which is a plain datetime.
        moment = pendulum.instance(context.get("logical_date") or context["dag_run"].run_after)
        return moment.in_timezone(VIENNA).date()

    # A poke that raises (the warehouse still starting after a reboot) is retried, unlike a
    # timeout; two hours of that is more than a container start takes.
    @task.sensor(
        poke_interval=WEATHER_POKE,
        timeout=WEATHER_PATIENCE,
        mode="reschedule",
        retries=12,
        retry_delay=timedelta(minutes=10),
    )
    def wait_for_weather(**context) -> bool:
        """Hold the delivery until the weather DAG has stored the day the readings cover."""
        return weather_ready(delivery_date_of(context))

    @task
    def produce_delivery_day(**context) -> int:
        """Register our metering points and produce everything that arrives on the run day."""
        delivery_date = delivery_date_of(context)
        registered, produced = deliver(delivery_date)
        print(f"{delivery_date}: registered {registered} new rows, produced {produced} messages")
        return produced

    # Produced is not stored: the consumer collects a batch for up to five seconds and inserts it
    # in one transaction, and a build started before that would miss the tail of the delivery.
    @task.sensor(
        poke_interval=timedelta(seconds=30),
        timeout=timedelta(hours=1),
        mode="reschedule",
        outlets=[METER_DELIVERY],
    )
    def wait_for_storage() -> bool:
        """The delivery counts once nothing of it is left on the topic."""
        return backlog() == 0

    wait_for_weather() >> produce_delivery_day() >> wait_for_storage()


meter_stream_daily()
