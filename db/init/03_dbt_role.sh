#!/bin/sh
# The account dbt uses. It reads raw and builds staging and marts, and can do nothing else:
# no INSERT into raw, so a mistaken hook, macro or package can never touch the history.
set -e

if [ -z "$WAREHOUSE_DBT_PASSWORD" ]; then
    echo "WAREHOUSE_DBT_PASSWORD is not set" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -v dbt_password="$WAREHOUSE_DBT_PASSWORD" \
     -v dbname="$POSTGRES_DB" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE megavolt_dbt LOGIN PASSWORD :'dbt_password';
GRANT USAGE ON SCHEMA raw TO megavolt_dbt;
GRANT SELECT ON ALL TABLES IN SCHEMA raw TO megavolt_dbt;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw GRANT SELECT ON TABLES TO megavolt_dbt;
GRANT USAGE, CREATE ON SCHEMA staging, marts TO megavolt_dbt;
-- Every role may create temporary tables by default. The pipeline never needs one.
REVOKE TEMPORARY ON DATABASE :"dbname" FROM PUBLIC;
GRANT TEMPORARY ON DATABASE :"dbname" TO megavolt_dbt;
-- A build that lost its client must not run on: give up a statement after the DAG's own limit,
-- and notice a dead connection within seconds instead of at the end of a long statement.
ALTER ROLE megavolt_dbt SET statement_timeout = '170min';
ALTER ROLE megavolt_dbt SET client_connection_check_interval = '15s';
SQL
