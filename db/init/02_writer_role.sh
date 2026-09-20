#!/bin/sh
# The account the pipeline uses. It may read and insert in raw and nothing else:
# no UPDATE, no DELETE, so the append-only rule is enforced by the database, not by habit.
set -e

if [ -z "$WAREHOUSE_WRITER_PASSWORD" ]; then
    echo "WAREHOUSE_WRITER_PASSWORD is not set" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -v writer_password="$WAREHOUSE_WRITER_PASSWORD" \
     -v dbname="$POSTGRES_DB" --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE megavolt_writer LOGIN PASSWORD :'writer_password';
GRANT USAGE ON SCHEMA raw TO megavolt_writer;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA raw TO megavolt_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw GRANT SELECT, INSERT ON TABLES TO megavolt_writer;
-- Every role may create temporary tables by default. The pipeline never needs one.
REVOKE TEMPORARY ON DATABASE :"dbname" FROM PUBLIC;
SQL
