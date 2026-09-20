-- Our whole portfolio per quarter hour: what was bought, what was delivered, what both cost, and
-- what a perfect forecast would have cost.
--
-- Comparisons against what was delivered use SETTLED customers only. A purchase is made for every
-- customer, but what a customer took is unknown until their reading arrives; adding the purchase
-- for all of them to the delivery of some would bias every comparison by the unsettled share.
-- The purchase for customers who are not settled yet is its own column.

select interval_start,
       delivery_day,
       bool_and(portfolio_is_complete)                                        as is_complete,
       count(*)                                                               as customers_bought_for,
       count(*) filter (where is_settled)                                     as customers_settled,
       sum(bought_kwh)                                                        as bought_kwh,
       sum(bought_kwh) filter (where is_settled)                              as bought_for_settled_kwh,
       sum(actual_kwh)                                                        as delivered_kwh,
       sum(imbalance_kwh)                                                     as imbalance_kwh,
       max(day_ahead_price_eur_mwh)                                           as day_ahead_price_eur_mwh,
       max(imbalance_price_eur_mwh)                                           as imbalance_price_eur_mwh,
       bool_and(imbalance_price_is_final)                                     as imbalance_price_is_final,
       sum(day_ahead_cost_eur) filter (where is_settled)                      as day_ahead_cost_eur,
       sum(imbalance_cost_eur)                                                as imbalance_cost_eur,
       sum(day_ahead_cost_eur) filter (where not is_settled)                  as unsettled_day_ahead_cost_eur,
       -- Everything that was delivered, bought at the day-ahead price: no imbalance at all.
       sum(actual_kwh / 1000 * day_ahead_price_eur_mwh)                       as perfect_forecast_cost_eur
from {{ ref('fct_customer_cost') }}
group by interval_start, delivery_day
