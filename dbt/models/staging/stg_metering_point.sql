-- Each registration of one of our metering points, valid until the next one takes over.
-- Meters change during the year, so a point has one row per meter it ever had.

with registration as (

    select distinct on (metering_point, valid_from)
           metering_point,
           valid_from,
           profile_type,
           segment,
           annual_kwh,
           meter_id
    from {{ source('raw', 'metering_point') }}
    -- The registry is append-only too: the latest registration for a valid_from wins.
    order by metering_point, valid_from, received_at desc, payload_hash desc

)

select metering_point,
       valid_from,
       lead(valid_from) over (partition by metering_point order by valid_from) as valid_to,
       profile_type,
       segment,
       annual_kwh,
       meter_id
from registration
