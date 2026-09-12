"""Load one year of official APCS synthetic load profiles into the warehouse.

Triggered by hand once a year rather than on a cron: APCS publishes a year's file when it is
ready, and a January schedule for a file that may not exist yet fails for weeks on end.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk import Param, dag, task

from megavolt.apcs import download, parse_profiles, profile_names
from megavolt.warehouse import store_load_profiles

VIENNA = "Europe/Vienna"


@dag(
    dag_id="apcs_load_profiles",
    schedule=None,
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=10)},
    params={
        "year": Param(
            2025,
            type="integer",
            minimum=2010,
            maximum=2100,
            title="Profile year",
            description="The calendar year the profiles describe. Its first interval starts "
            "on 31 December of the year before.",
        )
    },
    tags=["apcs", "profiles"],
)
def apcs_load_profiles():
    """One run per published year. Re-running the same year stores nothing new."""

    @task(execution_timeout=timedelta(minutes=30))
    def download_and_store(params=None) -> int:
        """Fetch the archive and write every quarter-hour value to raw.load_profile."""
        year = int(params["year"])
        archive = download(year)
        names = profile_names(archive)

        stored = store_load_profiles(parse_profiles(archive, year), year)
        print(f"{year}: {len(names)} profile types, stored {stored} new rows")
        return stored

    download_and_store()


apcs_load_profiles()
