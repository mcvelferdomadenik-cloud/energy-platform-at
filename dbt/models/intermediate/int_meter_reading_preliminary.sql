-- What we knew about each customer on the morning after delivery: the readings that had arrived by
-- 06:00 Vienna time on D+1. That is the first delivery for most customers; the ones that come one
-- to three days late are not here yet, and no correction is.
--
-- Same winner rule as stg_meter_reading, over fewer rows. Keep the two ORDER BY clauses identical.

{{ config(
    materialized='table',
    indexes=[{'columns': ['metering_point', 'interval_start'], 'unique': true}]
) }}

with known_by_the_next_morning as (

    select *
    from {{ source('raw', 'meter_reading') }}
    where delivered_at <= (
        ((interval_start at time zone 'Europe/Vienna')::date + 1)::timestamp + interval '6 hours'
    ) at time zone 'Europe/Vienna'

),

winning_reading as (

    select distinct on (metering_point, interval_start)
           metering_point,
           interval_start,
           consumption_kwh,
           allocated_kwh,
           version,
           delivered_at
    from known_by_the_next_morning
    order by metering_point, interval_start,
             version desc, delivered_at desc, payload_hash desc

)

select reading.metering_point,
       reading.interval_start,
       reading.consumption_kwh - reading.allocated_kwh as residual_kwh,
       reading.version,
       reading.delivered_at
from winning_reading as reading
-- The same registration rule as the final reading: only points that were ours at that moment.
join {{ ref('stg_metering_point') }} as period
  on period.metering_point = reading.metering_point
 and reading.interval_start >= period.valid_from
 and (period.valid_to is null or reading.interval_start < period.valid_to)
