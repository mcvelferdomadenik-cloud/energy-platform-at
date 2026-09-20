-- What each forecast method would have cost our whole portfolio, per quarter hour. Same customers,
-- same prices, same readings; only the forecast behind the purchase differs, so the difference
-- between two methods is what the better forecast is worth ON THIS DATA.
--
-- Read the ranking with care. The readings are simulated from the very load profiles the standard
-- profile method forecasts with, so that method is close to the process that made the data, and
-- the simulated weather has no memory from one day to the next. A method that learns from recent
-- days therefore cannot win here. That says how the simulator works; it says nothing about how
-- the two would compare on real meter data.
--
-- As everywhere, a purchase is compared with a delivery only for customers whose reading has
-- arrived. The direction that picks the imbalance price is each method's own: a method that buys
-- too little is short in a quarter hour where another is long.

with purchase as (

    select purchase.forecast_method,
           purchase.interval_start,
           purchase.bought_kwh,
           purchase.day_ahead_price_eur_mwh,
           actual.residual_kwh as actual_kwh
    from {{ ref('int_purchase') }} as purchase
    left join {{ ref('fct_community_allocation') }} as actual using (metering_point, interval_start)

),

portfolio as (

    select forecast_method,
           interval_start,
           count(*)                                              as customers_bought_for,
           count(actual_kwh)                                     as customers_settled,
           sum(bought_kwh) filter (where actual_kwh is not null) as bought_for_settled_kwh,
           sum(actual_kwh)                                       as delivered_kwh,
           sum(actual_kwh - bought_kwh)                          as imbalance_kwh,
           max(day_ahead_price_eur_mwh)                          as day_ahead_price_eur_mwh
    from purchase
    group by forecast_method, interval_start

)

select portfolio.forecast_method,
       portfolio.interval_start,
       (portfolio.interval_start at time zone 'Europe/Vienna')::date as delivery_day,
       portfolio.customers_bought_for,
       portfolio.customers_settled,
       portfolio.bought_for_settled_kwh,
       portfolio.delivered_kwh,
       portfolio.imbalance_kwh,
       portfolio.day_ahead_price_eur_mwh,
       case when portfolio.imbalance_kwh > 0 then price.short_price_eur_mwh
            else price.long_price_eur_mwh
       end                                                             as imbalance_price_eur_mwh,
       portfolio.bought_for_settled_kwh / 1000 * portfolio.day_ahead_price_eur_mwh
                                                                       as day_ahead_cost_eur,
       portfolio.imbalance_kwh / 1000
           * case when portfolio.imbalance_kwh > 0 then price.short_price_eur_mwh
                  else price.long_price_eur_mwh
             end                                                       as imbalance_cost_eur,
       -- Everything that was delivered, bought at the day-ahead price: no imbalance at all.
       portfolio.delivered_kwh / 1000 * portfolio.day_ahead_price_eur_mwh
                                                                       as perfect_forecast_cost_eur
from portfolio
left join {{ ref('int_imbalance_price_quarter_hour') }} as price using (interval_start)
