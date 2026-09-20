-- What would be bought day-ahead for each customer and quarter hour, under each forecast method.
--
-- This is the one place where a forecast becomes a purchase. The day-ahead auction sells one
-- quantity per auction period, so while the auction priced whole hours (until 30 September 2025)
-- the purchase is the hour's average for each of its four quarter hours: a forecast shaped within
-- the hour is flattened by the market, and that alone causes imbalance.

{{ config(
    materialized='table',
    indexes=[
        {'columns': ['forecast_method', 'metering_point', 'interval_start'], 'unique': true},
        {'columns': ['forecast_method', 'interval_start']},
    ]
) }}

with forecast as (

    select 'standard_profile' as forecast_method, metering_point, interval_start, forecast_residual_kwh
    from {{ ref('int_forecast_standard_profile') }}
    union all
    select 'persistence', metering_point, interval_start, forecast_residual_kwh
    from {{ ref('int_forecast_persistence') }}

)

select forecast.forecast_method,
       forecast.metering_point,
       forecast.interval_start,
       forecast.forecast_residual_kwh,
       price.price_eur_mwh       as day_ahead_price_eur_mwh,
       price.auction_resolution,
       -- The resolution is part of the period: an hour and a quarter hour that start together
       -- would otherwise share one average.
       avg(forecast.forecast_residual_kwh) over (
           partition by forecast.forecast_method,
                        forecast.metering_point,
                        price.auction_resolution,
                        date_bin(price.auction_resolution, forecast.interval_start,
                                 timestamptz '2000-01-01 00:00:00+00')
       ) as bought_kwh
from forecast
join {{ ref('int_day_ahead_price_quarter_hour') }} as price using (interval_start)
