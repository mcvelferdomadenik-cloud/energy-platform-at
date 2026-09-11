FROM apache/airflow:3.3.1@sha256:0c4bcc0370e526de1b7892a3bf4343d260c6c82359c66f77155b53cd773d6339

COPY --chown=airflow:root airflow/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir apache-airflow==3.3.1 -r /tmp/requirements.txt \
    --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.13.txt"

COPY --chown=airflow:root pyproject.toml README.md /opt/megavolt/
COPY --chown=airflow:root src /opt/megavolt/src
RUN pip install --no-cache-dir --no-deps /opt/megavolt
