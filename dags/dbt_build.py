"""Rebuild staging and marts every morning, after the meter delivery has landed.

`dbt build` runs models and their tests in dependency order and stops at the first failing test,
so a broken invariant never reaches a mart. dbt lives in its own venv in the image (see
airflow/Dockerfile) and connects as `megavolt_dbt`, which can read raw and write nothing there.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag

VIENNA = "Europe/Vienna"
PROJECT = "/opt/megavolt/dbt"
# dbt sees these and nothing else. The scheduler's environment also holds the writer DSN, the
# ENTSO-E token and Airflow's own keys, and a dbt package can read any variable with env_var().
DBT_ENV = ("WAREHOUSE_HOST", "WAREHOUSE_PORT", "WAREHOUSE_DBT_PASSWORD", "POSTGRES_DB")


@dag(
    dag_id="dbt_build",
    schedule="30 5 * * *",
    start_date=pendulum.datetime(2026, 9, 1, tz=VIENNA),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=10)},
    tags=["dbt", "warehouse"],
)
def dbt_build():
    """Half an hour after meter_stream_daily: the consumer needs seconds, not minutes."""

    BashOperator(
        task_id="dbt_build",
        # `env -i` starts from an empty environment; the shell fills in the four values at run time.
        bash_command=(
            "exec env -i PATH=/usr/bin:/bin HOME=/home/airflow "
            + " ".join(f'{name}="${name}"' for name in DBT_ENV)
            + f" /opt/dbt/bin/dbt build --project-dir {PROJECT} --profiles-dir {PROJECT}"
            " --no-use-colors --fail-fast"
        ),
    )


dbt_build()
