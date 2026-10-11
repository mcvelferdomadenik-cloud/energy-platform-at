-- Forecast v0: the standard load profile method, for every customer and every quarter hour we can
-- buy for. It is the official Austrian method for customers without a measured forecast: a
-- customer's annual consumption times the published APCS profile of their class (the profiles sum
-- to 1000 over a year). Source of the profiles: apcs.at, synthetic load profiles.
--
-- What the community is expected to cover comes from the same kind of reasoning:
--   expected generation  = plant size x annual yield / 1000 x the APCS photovoltaic profile E1
--   expected consumption = our customers' forecast, scaled to the whole community
-- The scaling assumes the members who are not our customers consume in the same shape as ours. We
-- never see them, so nothing better is known on the day before delivery.
--
-- It uses no weather and no readings, so it is the same on a sunny and on an overcast day. That is
-- the point of a baseline: the imbalance it causes is what a better forecast is measured against.
--
-- The horizon is the days we deliver on: every local day with a reading, and the day after it
-- (tomorrow's purchase is made before tomorrow's readings exist). Prices reach back years further,
-- but a forecast for a day we never delivered has nothing to be settled against, and 350 customers
-- times two years of quarter hours is what made the purchase model unbuildable on a laptop.

-- Incremental: the forecast for a day never changes once made; a run adds the days that gained
-- their first reading since the last run (late readings open a day up to 3 days later; 16 is safe).
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key=['metering_point', 'interval_start'],
    indexes=[
        {'columns': ['metering_point', 'interval_start'], 'unique': true},
        {'columns': ['interval_start']},
    ]
) }}

with delivery_day as (

    select distinct (interval_start at time zone 'Europe/Vienna')::date as local_day
    from {{ ref('stg_meter_reading') }}
    where {{ recent_only('interval_start', 16) }}

),

horizon as (

    select local_day from delivery_day
    union
    select local_day + 1 from delivery_day

),

quarter_hour as (

    -- Only quarter hours with a day-ahead price: there is nothing to buy for the others.
    select price.interval_start
    from {{ ref('int_day_ahead_price_quarter_hour') }} as price
    join horizon
      on (price.interval_start at time zone 'Europe/Vienna')::date = horizon.local_day

),

customer as (

    -- Who was our customer at that quarter hour, with the class and size registered then.
    select quarter_hour.interval_start,
           registration.metering_point,
           registration.profile_type,
           registration.annual_kwh
    from quarter_hour
    join {{ ref('stg_metering_point') }} as registration
      on quarter_hour.interval_start >= registration.valid_from
     and (registration.valid_to is null or quarter_hour.interval_start < registration.valid_to)

),

consumption as (

    select customer.interval_start,
           customer.metering_point,
           customer.annual_kwh,
           customer.annual_kwh / 1000 * shape.value as forecast_consumption_kwh
    from customer
    join {{ ref('stg_load_profile') }} as shape
      on shape.profile_type = customer.profile_type
     and shape.interval_start = customer.interval_start

),

community as (

    select ours.interval_start,
           {{ var('community_plant_kwp') }} * {{ var('community_yield_kwh_per_kwp') }} / 1000.0
               * sun.value as expected_generation_kwh,
           ours.forecast_consumption_kwh
               * {{ var('community_annual_kwh') }} / ours.annual_kwh as expected_consumption_kwh
    from (
        select interval_start,
               sum(forecast_consumption_kwh) as forecast_consumption_kwh,
               sum(annual_kwh)               as annual_kwh
        from consumption
        group by interval_start
    ) as ours
    join {{ ref('stg_load_profile') }} as sun
      on sun.profile_type = 'E1'
     and sun.interval_start = ours.interval_start

)

select consumption.metering_point,
       consumption.interval_start,
       consumption.forecast_consumption_kwh,
       case when community.expected_consumption_kwh = 0 then 1
            else least(1, community.expected_generation_kwh / community.expected_consumption_kwh)
       end as expected_coverage_ratio,
       consumption.forecast_consumption_kwh * (1 - case
           when community.expected_consumption_kwh = 0 then 1
           else least(1, community.expected_generation_kwh / community.expected_consumption_kwh)
       end) as forecast_residual_kwh
from consumption
join community using (interval_start)
