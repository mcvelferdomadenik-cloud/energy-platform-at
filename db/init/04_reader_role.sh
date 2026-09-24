#!/bin/sh
# The account the internal pricing tool reads with. It may SELECT the models dbt builds and
# nothing else: no write anywhere, no direct access to raw. Note what "staging" means: its views
# run with their owner's rights (megavolt_dbt reads raw), so through them the reader sees every
# accepted reading; that is the intent, staging is the cleaned data. dbt drops and recreates its
# tables on every build, so the grant is a default privilege on what megavolt_dbt creates.
set -e

if [ -z "$WAREHOUSE_READER_PASSWORD" ]; then
    echo "WAREHOUSE_READER_PASSWORD is not set" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -v reader_password="$WAREHOUSE_READER_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE megavolt_reader LOGIN PASSWORD :'reader_password';
GRANT USAGE ON SCHEMA staging, marts TO megavolt_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA staging, marts TO megavolt_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE megavolt_dbt IN SCHEMA staging, marts
    GRANT SELECT ON TABLES TO megavolt_reader;
SQL
