-- What would be bought day-ahead for each customer and quarter hour, under each forecast method.
--
-- This is the one place where a forecast becomes a purchase. The day-ahead auction sells one
-- quantity per auction period, so while the auction priced whole hours (until 30 September 2025)
-- the purchase is the hour's average for each of its four quarter hours: a forecast shaped within
-- the hour is flattened by the market, and that alone causes imbalance.
--
-- The average is taken with a GROUP BY and joined back, not with a window function: a window
-- needs every row sorted by its period, and at 25 million rows that sort ran for hours on disk.
-- The grouped form is a hash aggregate that fits in the memory the pre-hook grants this session.

-- Incremental: a run reworks the last 16 days, the forecasts' own window.
{{ config(
    materialized='incremental',
    incremental_strategy='delete+insert',
    unique_key=['forecast_method', 'metering_point', 'interval_start'],
    pre_hook="set local work_mem = '512MB'",
    indexes=[
        {'columns': ['forecast_method', 'metering_point', 'interval_start'], 'unique': true},
        {'columns': ['forecast_method', 'interval_start']},
    ]
) }}

with forecast as (

    select 'standard_profile' as forecast_method, metering_point, interval_start, forecast_residual_kwh
    from {{ ref('int_forecast_standard_profile') }}
    where {{ recent_only('interval_start', 16) }}
    union all
    select 'persistence', metering_point, interval_start, forecast_residual_kwh
    from {{ ref('int_forecast_persistence') }}
    where {{ recent_only('interval_start', 16) }}

),

priced as (

    select forecast.forecast_method,
           forecast.metering_point,
           forecast.interval_start,
           forecast.forecast_residual_kwh,
           price.price_eur_mwh       as day_ahead_price_eur_mwh,
           price.auction_resolution,
           -- The resolution is part of the period: an hour and a quarter hour that start together
           -- would otherwise share one average.
           date_bin(price.auction_resolution, forecast.interval_start,
                    timestamptz '2000-01-01 00:00:00+00') as period_start
    from forecast
    join {{ ref('int_day_ahead_price_quarter_hour') }} as price using (interval_start)

),

period as (

    select forecast_method, metering_point, auction_resolution, period_start,
           avg(forecast_residual_kwh) as bought_kwh
    from priced
    group by 1, 2, 3, 4

)

select priced.forecast_method,
       priced.metering_point,
       priced.interval_start,
       priced.forecast_residual_kwh,
       priced.day_ahead_price_eur_mwh,
       priced.auction_resolution,
       period.bought_kwh
from priced
join period using (forecast_method, metering_point, auction_resolution, period_start)
