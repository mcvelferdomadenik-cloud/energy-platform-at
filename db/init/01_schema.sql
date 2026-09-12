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

-- The official APCS shapes, one year per download. Read whole profiles by type rather than
-- by time range, so deliberately not a hypertable. profile_year is stored because it cannot
-- be derived: the 2025 profile's first interval starts on 31 December 2024.
CREATE TABLE raw.load_profile (
    profile_type   text             NOT NULL,
    interval_start timestamptz      NOT NULL,
    profile_year   integer          NOT NULL,
    value          double precision NOT NULL,
    source         text             NOT NULL DEFAULT 'apcs',
    received_at    timestamptz      NOT NULL DEFAULT now(),
    payload_hash   text             NOT NULL,
    PRIMARY KEY (profile_type, interval_start, payload_hash),
    CHECK (value >= 0)
);

CREATE TABLE raw.metering_point (
    metering_point text             NOT NULL,
    valid_from     timestamptz      NOT NULL,
    profile_type   text             NOT NULL,
    segment        text             NOT NULL,
    annual_kwh     double precision NOT NULL,
    meter_id       text             NOT NULL,
    source         text             NOT NULL DEFAULT 'simulator',
    received_at    timestamptz      NOT NULL DEFAULT now(),
    payload_hash   text             NOT NULL,
    PRIMARY KEY (metering_point, valid_from, payload_hash),
    CHECK (annual_kwh > 0)
);

-- Our customers only. A correction is a new row with a higher version, never an update.
-- A missing interval is an absent row, never a zero.
CREATE TABLE raw.meter_reading (
    metering_point  text             NOT NULL,
    interval_start  timestamptz      NOT NULL,
    consumption_kwh double precision NOT NULL,
    allocated_kwh   double precision NOT NULL,
    meter_id        text             NOT NULL,
    version         integer          NOT NULL,
    delivered_at    timestamptz      NOT NULL,
    source          text             NOT NULL DEFAULT 'meter_stream',
    received_at     timestamptz      NOT NULL DEFAULT now(),
    payload_hash    text             NOT NULL,
    PRIMARY KEY (metering_point, interval_start, payload_hash),
    CHECK (consumption_kwh >= 0),
    CHECK (allocated_kwh >= 0),
    CHECK (allocated_kwh <= consumption_kwh),
    CHECK (version >= 1)
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'interval_start'
);

-- What the community reports about itself: totals only, no member breakdown.
CREATE TABLE raw.community_interval (
    interval_start  timestamptz      NOT NULL,
    generation_kwh  double precision NOT NULL,
    consumption_kwh double precision NOT NULL,
    version         integer          NOT NULL,
    delivered_at    timestamptz      NOT NULL,
    source          text             NOT NULL DEFAULT 'meter_stream',
    received_at     timestamptz      NOT NULL DEFAULT now(),
    payload_hash    text             NOT NULL,
    PRIMARY KEY (interval_start, payload_hash),
    CHECK (generation_kwh >= 0),
    CHECK (consumption_kwh >= 0),
    CHECK (version >= 1)
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'interval_start'
);
