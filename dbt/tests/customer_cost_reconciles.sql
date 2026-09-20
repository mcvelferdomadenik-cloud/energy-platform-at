-- The cost attributed to customers must be the cost of the portfolio. Returns the quarter hours
-- where it is not.
--
-- Every right-hand side is computed on its own path, from other models, so nothing here is a sum
-- compared with itself:
--   purchase    the auction period's average of the TOTAL forecast, from the forecast model
--   imbalance   what customers took, from the allocation mart, minus what was bought for them
--   price       the long or the short price from staging, by the sign of that imbalance
-- A customer doubled in a join, an average over the wrong period, a wrong sign or the wrong
-- direction's price all part the two sides. A customer missing from the forecast altogether does
-- not, because both sides would miss them; every_reading_has_a_cost covers that.

with attributed as (

    select interval_start,
           sum(bought_kwh)                                  as bought_kwh,
           sum(bought_kwh) filter (where is_settled)        as bought_for_settled_kwh,
           sum(day_ahead_cost_eur)                          as day_ahead_cost_eur,
           sum(imbalance_kwh)                               as imbalance_kwh,
           sum(imbalance_cost_eur)                          as imbalance_cost_eur,
           max(day_ahead_price_eur_mwh)                     as day_ahead_price_eur_mwh,
           min(day_ahead_price_eur_mwh)                     as lowest_day_ahead_price_eur_mwh,
           max(imbalance_price_eur_mwh)                     as imbalance_price_eur_mwh,
           min(imbalance_price_eur_mwh)                     as lowest_imbalance_price_eur_mwh
    from {{ ref('fct_customer_cost') }}
    group by interval_start

),

total_forecast as (

    select forecast.interval_start,
           price.auction_resolution,
           sum(forecast.forecast_residual_kwh) as forecast_kwh
    from {{ ref('int_forecast_standard_profile') }} as forecast
    join {{ ref('int_day_ahead_price_quarter_hour') }} as price using (interval_start)
    group by forecast.interval_start, price.auction_resolution

),

portfolio as (

    select interval_start,
           avg(forecast_kwh) over (
               partition by auction_resolution,
                            date_bin(auction_resolution, interval_start,
                                     timestamptz '2000-01-01 00:00:00+00')
           ) as bought_kwh
    from total_forecast

),

delivered as (

    select allocation.interval_start,
           sum(allocation.residual_kwh) as delivered_kwh
    from {{ ref('fct_community_allocation') }} as allocation
    join {{ ref('fct_customer_cost') }} as cost using (metering_point, interval_start)
    group by allocation.interval_start

),

imbalance_price as (

    select interval_start,
           max(price_eur_mwh) filter (where direction = 'long')  as long_price_eur_mwh,
           max(price_eur_mwh) filter (where direction = 'short') as short_price_eur_mwh
    from {{ ref('stg_imbalance_price') }}
    group by interval_start

)

select attributed.*,
       portfolio.bought_kwh    as portfolio_bought_kwh,
       delivered.delivered_kwh as portfolio_delivered_kwh
from attributed
join portfolio using (interval_start)
left join delivered using (interval_start)
left join imbalance_price using (interval_start)
where abs(attributed.bought_kwh - portfolio.bought_kwh) > 1e-6
   or abs(attributed.day_ahead_cost_eur
          - portfolio.bought_kwh / 1000 * attributed.day_ahead_price_eur_mwh) > 1e-6
   -- what customers took, minus what was bought for those same customers
   or abs(attributed.imbalance_kwh
          - (delivered.delivered_kwh - attributed.bought_for_settled_kwh)) > 1e-6
   -- short pays the short price, long is paid the long one
   or attributed.imbalance_price_eur_mwh is distinct from
          case when delivered.delivered_kwh - attributed.bought_for_settled_kwh > 0
               then imbalance_price.short_price_eur_mwh
               else imbalance_price.long_price_eur_mwh
          end
   or abs(attributed.imbalance_cost_eur
          - (delivered.delivered_kwh - attributed.bought_for_settled_kwh) / 1000
            * attributed.imbalance_price_eur_mwh) > 1e-6
   -- One price per quarter hour for everybody.
   or attributed.day_ahead_price_eur_mwh <> attributed.lowest_day_ahead_price_eur_mwh
   or attributed.imbalance_price_eur_mwh <> attributed.lowest_imbalance_price_eur_mwh
