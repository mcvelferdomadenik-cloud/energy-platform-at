-- One row per quarter hour: what our customers took from us after the community's share, next to
-- what the community as a whole generated, consumed, shared and sent back to the grid.
-- This is the ACTUAL side of our position; the forecast, the purchase and the cost join it later.

with ours as (

    select interval_start,
           delivery_day,
           count(*)              as customers_reporting,
           sum(consumption_kwh)  as our_consumption_kwh,
           sum(allocated_kwh)    as our_allocated_kwh,
           sum(residual_kwh)     as our_residual_kwh
    from {{ ref('fct_community_allocation') }}
    group by interval_start, delivery_day

),

registered as (

    -- Who was our customer AT that quarter hour, not who is today: a customer who joins later
    -- must not make every earlier quarter hour look incomplete. A point has one registration
    -- valid at a time, so this counts customers, not meters.
    select ours.interval_start,
           count(*) as customers_registered
    from ours
    join {{ ref('stg_metering_point') }} as registration
      on ours.interval_start >= registration.valid_from
     and (registration.valid_to is null or ours.interval_start < registration.valid_to)
    group by ours.interval_start

),

community as (

    select interval_start,
           generation_kwh,
           consumption_kwh,
           -- The community shares what it generates up to what it consumes; the rest goes to the grid.
           least(generation_kwh, consumption_kwh)        as allocated_kwh,
           greatest(0, generation_kwh - consumption_kwh) as surplus_kwh
    from {{ ref('stg_community_interval') }}

)

select ours.interval_start,
       ours.delivery_day,
       registered.customers_registered,
       ours.customers_reporting,
       -- Readings arrive up to three days late, so a recent quarter hour is rarely complete.
       ours.customers_reporting = registered.customers_registered as is_complete,
       ours.our_consumption_kwh,
       ours.our_allocated_kwh,
       ours.our_residual_kwh,
       community.generation_kwh   as community_generation_kwh,
       community.consumption_kwh  as community_consumption_kwh,
       community.allocated_kwh    as community_allocated_kwh,
       community.surplus_kwh      as community_surplus_kwh
from ours
join registered using (interval_start)
left join community using (interval_start)
