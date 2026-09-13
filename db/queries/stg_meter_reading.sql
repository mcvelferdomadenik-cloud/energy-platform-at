-- One true reading per metering point per quarter hour, until dbt takes this over as a model.
--
-- raw.meter_reading keeps every delivery, including first deliveries that were later corrected.
-- This picks the winner and attaches the registry row that was valid at that moment.
--
-- A correction only carries the intervals it changes; the rest are absent rows, not nulls. So on
-- a partially corrected day, version 1 still wins wherever no version 2 row exists.

WITH winning_reading AS (
    SELECT DISTINCT ON (metering_point, interval_start)
           metering_point,
           interval_start,
           consumption_kwh,
           allocated_kwh,
           meter_id,
           version,
           delivered_at
    FROM raw.meter_reading
    -- Highest version wins; a later delivery breaks a tie; payload_hash makes the order total, so
    -- two sends of the same version with different values still resolve the same way every run.
    ORDER BY metering_point, interval_start,
             version DESC, delivered_at DESC, payload_hash DESC
),

registry AS (
    -- The registry is append-only too: the latest registration for a valid_from wins.
    SELECT DISTINCT ON (metering_point, valid_from)
           metering_point,
           valid_from,
           profile_type,
           segment,
           meter_id
    FROM raw.metering_point
    ORDER BY metering_point, valid_from, received_at DESC, payload_hash DESC
),

registry_period AS (
    -- Each registration is valid until the next one for the same point takes over.
    SELECT metering_point,
           valid_from,
           lead(valid_from) OVER (PARTITION BY metering_point ORDER BY valid_from) AS valid_to,
           profile_type,
           segment,
           meter_id
    FROM registry
)

SELECT reading.metering_point,
       reading.interval_start,
       reading.consumption_kwh,
       reading.allocated_kwh,
       reading.version,
       reading.delivered_at,
       reading.meter_id,
       period.meter_id                       AS registered_meter_id,
       reading.meter_id = period.meter_id    AS meter_matches_registry,
       period.profile_type,
       period.segment
FROM winning_reading AS reading
-- Inner join: a reading for a point that was never registered as ours does not belong in our models.
JOIN registry_period AS period
  ON period.metering_point = reading.metering_point
 AND reading.interval_start >= period.valid_from
 AND (period.valid_to IS NULL OR reading.interval_start < period.valid_to)
