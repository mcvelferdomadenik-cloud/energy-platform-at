-- One true reading per metering point per quarter hour.
--
-- raw.meter_reading keeps every delivery, including first deliveries that were later corrected.
-- This picks the winner and attaches the registration that was valid at that moment.
--
-- A correction only carries the intervals it changes; the rest are absent rows, not nulls. So on
-- a partially corrected day, version 1 still wins wherever no version 2 row exists.

with winning_reading as (

    select distinct on (metering_point, interval_start)
           metering_point,
           interval_start,
           consumption_kwh,
           allocated_kwh,
           meter_id,
           version,
           delivered_at
    from {{ source('raw', 'meter_reading') }}
    -- Highest version wins; a later delivery breaks a tie; payload_hash makes the order total, so
    -- two sends of the same version with different values still resolve the same way every run.
    order by metering_point, interval_start,
             version desc, delivered_at desc, payload_hash desc

)

select reading.metering_point,
       reading.interval_start,
       reading.consumption_kwh,
       reading.allocated_kwh,
       reading.version,
       reading.delivered_at,
       reading.meter_id,
       period.meter_id                       as registered_meter_id,
       reading.meter_id = period.meter_id    as meter_matches_registry,
       period.profile_type,
       period.segment
from winning_reading as reading
-- Inner join: a reading for a point that was never registered as ours does not belong in our models.
join {{ ref('stg_metering_point') }} as period
  on period.metering_point = reading.metering_point
 and reading.interval_start >= period.valid_from
 and (period.valid_to is null or reading.interval_start < period.valid_to)
