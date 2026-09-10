-- Runs once when the warehouse volume is created.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;

-- Append-only. A correction is a new row with a different payload_hash, never an update.
CREATE TABLE raw.day_ahead_price (
    interval_start timestamptz      NOT NULL,
    bidding_zone   text             NOT NULL,
    resolution     interval         NOT NULL,
    price_eur_mwh  double precision NOT NULL,
    source         text             NOT NULL DEFAULT 'entsoe',
    received_at    timestamptz      NOT NULL DEFAULT now(),
    payload_hash   text             NOT NULL,
    PRIMARY KEY (bidding_zone, interval_start, payload_hash)
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'interval_start'
);
