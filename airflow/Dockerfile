FROM apache/airflow:3.3.1@sha256:0c4bcc0370e526de1b7892a3bf4343d260c6c82359c66f77155b53cd773d6339

COPY --chown=airflow:root airflow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir apache-airflow==3.3.1 -r /tmp/requirements.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.13.txt"

# dbt cannot share the Airflow environment: its dependencies collide with the Airflow constraints
# (pathspec), so it gets a venv of its own. It is installed straight from uv.lock, the same lock
# the laptop uses, and uv checks every package against the hashes recorded there.
COPY --from=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 /uv /usr/local/bin/uv
USER root
RUN mkdir /opt/dbt /opt/dbt-lock && chown airflow:root /opt/dbt /opt/dbt-lock
USER airflow
COPY --chown=airflow:root pyproject.toml uv.lock /opt/dbt-lock/
RUN cd /opt/dbt-lock \
    && UV_PROJECT_ENVIRONMENT=/opt/dbt UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 \
       uv sync --locked --only-group dbt --python "$(command -v python)"

# Packages are installed from package-lock.yml at build time, so the image never depends on
# what happens to be on the laptop.
COPY --chown=airflow:root dbt /opt/megavolt/dbt
RUN /opt/dbt/bin/dbt deps --project-dir /opt/megavolt/dbt --profiles-dir /opt/megavolt/dbt

COPY --chown=airflow:root pyproject.toml README.md /opt/megavolt/
COPY --chown=airflow:root src /opt/megavolt/src
RUN pip install --no-cache-dir --no-deps /opt/megavolt
