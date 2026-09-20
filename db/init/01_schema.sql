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
    -- received_at is in the key because a value can return to an earlier one (A, B, A), and the
    -- third delivery must be kept: the latest row wins in staging.
    PRIMARY KEY (bidding_zone, interval_start, payload_hash, received_at),
    -- An upper bound also refuses NaN, which Postgres sorts above every number.
    CHECK (price_eur_mwh BETWEEN -100000 AND 100000),
    -- A model spreads each price over its resolution in quarter-hour steps, so an absurd
    -- resolution would multiply one row into millions, and a zero one would make it vanish.
    CHECK (resolution BETWEEN interval '15 minutes' AND interval '1 day'),
    -- Whole quarter hours, starting on one: 20 minutes would leave five of them unpriced, and 50
    -- would run into the next price.
    CONSTRAINT day_ahead_price_on_the_quarter_hour_grid CHECK (
        extract(epoch from resolution)::bigint % 900 = 0
        AND extract(epoch from interval_start)::bigint % 900 = 0
    )
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
    -- An upper bound also refuses NaN and Infinity, which Postgres sorts above every number.
    CHECK (value >= 0 AND value < 1000000)
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
    -- An upper bound also refuses NaN and Infinity, which Postgres sorts above every number.
    CHECK (annual_kwh > 0 AND annual_kwh < 1000000000)
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
    -- An upper bound also refuses NaN and Infinity, which Postgres sorts above every number.
    CHECK (consumption_kwh >= 0 AND consumption_kwh < 1000000),
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
    -- An upper bound also refuses NaN and Infinity, which Postgres sorts above every number.
    CHECK (generation_kwh >= 0 AND generation_kwh < 1000000),
    CHECK (consumption_kwh >= 0 AND consumption_kwh < 1000000),
    CHECK (version >= 1)
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'interval_start'
);

-- Imbalance prices are revised for days after delivery: intermediate (A01) first, final (A02)
-- later, so the status is part of the row. Both directions are kept (A04 long, A05 short),
-- although Austria has priced them identically in every day checked so far.
CREATE TABLE raw.imbalance_price (
    interval_start timestamptz      NOT NULL,
    control_area   text             NOT NULL,
    category       text             NOT NULL,
    doc_status     text             NOT NULL,
    resolution     interval         NOT NULL,
    price_eur_mwh  double precision NOT NULL,
    source         text             NOT NULL DEFAULT 'entsoe',
    received_at    timestamptz      NOT NULL DEFAULT now(),
    payload_hash   text             NOT NULL,
    -- received_at is in the key because a value can return to an earlier one (A, B, A), and the
    -- third delivery must be kept: the latest row wins in staging.
    PRIMARY KEY (control_area, interval_start, category, payload_hash, received_at),
    -- An upper bound also refuses NaN, which Postgres sorts above every number.
    CHECK (price_eur_mwh BETWEEN -100000 AND 100000),
    CHECK (category IN ('A04', 'A05')),
    CHECK (doc_status IN ('A01', 'A02'))
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'interval_start'
);

-- Actual total load of the bidding zone, in MW, as ENTSO-E publishes it.
CREATE TABLE raw.actual_load (
    interval_start timestamptz      NOT NULL,
    bidding_zone   text             NOT NULL,
    resolution     interval         NOT NULL,
    load_mw        double precision NOT NULL,
    source         text             NOT NULL DEFAULT 'entsoe',
    received_at    timestamptz      NOT NULL DEFAULT now(),
    payload_hash   text             NOT NULL,
    PRIMARY KEY (bidding_zone, interval_start, payload_hash, received_at),
    CHECK (load_mw >= 0 AND load_mw < 1000000)
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'interval_start'
);

-- Hourly weather for one point, from GeoSphere Austria (INCA analysis, CC BY 4.0). One row per
-- parameter and hour. A missing hour is an absent row, never a zero. The analysis can be
-- reprocessed, so received_at is in the key for the same reason as in raw.imbalance_price.
CREATE TABLE raw.weather_observation (
    valid_at     timestamptz      NOT NULL,
    dataset      text             NOT NULL,
    latitude     double precision NOT NULL,
    longitude    double precision NOT NULL,
    parameter    text             NOT NULL,
    value        double precision NOT NULL,
    source       text             NOT NULL DEFAULT 'geosphere',
    received_at  timestamptz      NOT NULL DEFAULT now(),
    payload_hash text             NOT NULL,
    PRIMARY KEY (dataset, latitude, longitude, parameter, valid_at, payload_hash, received_at),
    -- Each parameter has its own physical range, because raw is append-only: a row that gets
    -- in cannot be taken out again. An upper bound also refuses NaN.
    CONSTRAINT weather_value_in_range CHECK (
        (parameter = 'T2M' AND value BETWEEN -60 AND 50)
        OR (parameter = 'GL' AND value BETWEEN 0 AND 1400)
    )
) WITH (
    timescaledb.hypertable,
    timescaledb.partition_column = 'valid_at'
);
