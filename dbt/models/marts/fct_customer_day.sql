-- One row per customer per Austrian day: what arrived, what is still missing, and the volume
-- we supplied after the community took its share. A missing quarter hour is a lower count,
-- never a zero.

with per_day as (

    select metering_point,
           delivery_day,
           count(*)             as intervals_received,
           sum(consumption_kwh) as consumption_kwh,
           sum(allocated_kwh)   as allocated_kwh,
           max(version)         as latest_version,
           max(delivered_at)    as last_delivered_at
    from {{ ref('fct_community_allocation') }}
    group by metering_point, delivery_day

),

expected as (

    select *,
           -- A Vienna day is 92, 96 or 100 quarter hours; the clock change decides which.
           (extract(epoch from (delivery_day + 1)::timestamp at time zone 'Europe/Vienna'
                             -  delivery_day::timestamp      at time zone 'Europe/Vienna')
            / 900)::integer as intervals_expected
    from per_day

)

select metering_point,
       delivery_day,
       intervals_expected,
       intervals_received,
       intervals_received = intervals_expected as is_complete,
       consumption_kwh,
       allocated_kwh,
       consumption_kwh - allocated_kwh         as residual_kwh,
       latest_version,
       last_delivered_at
from expected
