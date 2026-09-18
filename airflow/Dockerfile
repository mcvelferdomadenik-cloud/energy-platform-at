FROM apache/airflow:3.3.1@sha256:0c4bcc0370e526de1b7892a3bf4343d260c6c82359c66f77155b53cd773d6339

COPY --chown=airflow:root airflow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir apache-airflow==3.3.1 -r /tmp/requirements.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.13.txt"

# dbt cannot share the Airflow environment: its dependencies collide with the Airflow constraints
# (pathspec), so it gets a venv of its own, installed from uv.lock's versions and hashes.
USER root
RUN mkdir /opt/dbt && chown airflow:root /opt/dbt
USER airflow
COPY --chown=airflow:root airflow/requirements-dbt.txt /tmp/requirements-dbt.txt
RUN python -m venv /opt/dbt \
    && /opt/dbt/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements-dbt.txt

# Packages are installed from package-lock.yml at build time, so the image never depends on
# what happens to be on the laptop.
COPY --chown=airflow:root dbt /opt/megavolt/dbt
RUN /opt/dbt/bin/dbt deps --project-dir /opt/megavolt/dbt --profiles-dir /opt/megavolt/dbt

COPY --chown=airflow:root pyproject.toml README.md /opt/megavolt/
COPY --chown=airflow:root src /opt/megavolt/src
RUN pip install --no-cache-dir --no-deps /opt/megavolt
