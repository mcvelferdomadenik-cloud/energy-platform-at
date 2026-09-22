-- Forecast v1: persistence. What a customer took from us at this time of day on recent days of the
-- same kind (working day, Saturday, Sunday), averaged over the last two weeks.
--
-- The purchase for delivery day D is made around noon on D-1. What is known then are the readings
-- that had arrived by then: day D-2 and earlier, without the ones still running late and without
-- corrections. This model therefore reads only int_meter_reading_preliminary, what was known on the
-- morning after each day, and only days up to D-2. That is a little pessimistic (a reading that
-- came a day late is known by then and is not used) and it can never look into the future.
--
-- It forecasts what the customer took AFTER the community's share, so the weather of the last weeks
-- is in it, which the standard profile knows nothing about: the simulated plant follows the
-- radiation that was really measured, and a sunny or a cold spell lasts as long as it really did.
-- What it cannot know is tomorrow: an average of two weeks lags behind a season that is turning and
-- is wrong on the first overcast day after a sunny week.
--
-- Days are matched by LOCAL time, because consumption follows the clock. The sun does not: for two
-- weeks after a clock change the history is one solar hour off. Public holidays are not treated as
-- Sundays. Where a customer has no history yet, the standard profile stands in.

{{ config(
    materialized='table',
    indexes=[
        {'columns': ['metering_point', 'interval_start'], 'unique': true},
        {'columns': ['interval_start']},
    ]
) }}

with target as (

    -- Every customer and quarter hour the standard profile has a forecast for.
    select metering_point,
           interval_start,
           forecast_residual_kwh                                    as standard_profile_kwh,
           (interval_start at time zone 'Europe/Vienna')::date      as local_day,
           (interval_start at time zone 'Europe/Vienna')::time      as local_time
    from {{ ref('int_forecast_standard_profile') }}

),

history as (

    select metering_point,
           residual_kwh,
           (interval_start at time zone 'Europe/Vienna')::date      as local_day,
           (interval_start at time zone 'Europe/Vienna')::time      as local_time
    from {{ ref('int_meter_reading_preliminary') }}

),

target_and_history as (

    select metering_point, local_day, local_time, interval_start, standard_profile_kwh,
           null::double precision as residual_kwh
    from target
    union all
    select metering_point, local_day, local_time, null, null, residual_kwh
    from history

),

averaged as (

    select *,
           -- isodow runs from Monday = 1 to Sunday = 7; greatest(.., 5) folds the working days into
           -- one kind and leaves Saturday and Sunday their own.
           avg(residual_kwh) over similar_days   as persistence_kwh,
           count(residual_kwh) over similar_days as history_quarter_hours
    from target_and_history
    window similar_days as (
        partition by metering_point, local_time, greatest(extract(isodow from local_day), 5)
        order by local_day
        -- From two weeks back up to two days back: yesterday's readings arrive tonight. A longer
        -- window is steadier and lags more behind a season that is turning; two weeks is a plain
        -- round choice for a persistence baseline, fixed in advance and not tuned to the data.
        range between interval '15 days' preceding and interval '2 days' preceding
    )

)

select metering_point,
       interval_start,
       coalesce(persistence_kwh, standard_profile_kwh) as forecast_residual_kwh,
       history_quarter_hours,
       persistence_kwh is null                         as fell_back_to_standard_profile
from averaged
where interval_start is not null
