-- What each customer cost us in each quarter hour, and why.
--
--     day-ahead cost = bought x day-ahead price
--     imbalance cost = (actual - bought) x imbalance price
--
-- bought    The purchase forecast for this customer. The day-ahead auction sells one quantity per
--           auction period, so while the auction priced whole hours (until 30 September 2025) the
--           purchase is the hour's average for each of its four quarter hours. A forecast shaped
--           within the hour is flattened by the market, and that alone causes imbalance.
-- actual    What the customer took from us: consumption minus the community's share. Unknown while
--           the reading has not arrived; then the imbalance is unknown too, never zero.
-- price     Which imbalance price applies depends on the direction of OUR WHOLE portfolio in that
--           quarter hour, not on the customer's: short when we delivered more than we bought.
--
-- A customer is attributed the purchase forecast for them and the imbalance they caused: one of
-- several textbook cost-causation rules, chosen for this fictional supplier. The sum over
-- customers equals the portfolio, which dbt/tests/customer_cost_reconciles.sql asserts.
--
-- Method: imbalance settlement of a balance responsible party, Regulation (EU) 2017/2195
-- (Electricity Balancing Guideline), articles 52 to 55. The day-ahead market time unit has been
-- 15 minutes in the single day-ahead coupling since 1 October 2025.

{{ config(indexes=[
    {'columns': ['metering_point', 'interval_start'], 'unique': true},
    {'columns': ['interval_start']},
]) }}

with purchase as (

    -- The forecast we operate on is the standard profile, until a better one is chosen; what each
    -- method would have cost is compared in fct_forecast_comparison. The rule that turns a forecast
    -- into a purchase lives in int_purchase, for every method alike.
    select metering_point,
           interval_start,
           forecast_method,
           forecast_residual_kwh,
           day_ahead_price_eur_mwh,
           auction_resolution,
           bought_kwh
    from {{ ref('int_purchase') }}
    where forecast_method = 'standard_profile'

),

imbalance_price as (

    select * from {{ ref('int_imbalance_price_quarter_hour') }}

),

settled as (

    select purchase.*,
           actual.residual_kwh                       as actual_kwh,
           actual.residual_kwh - purchase.bought_kwh as imbalance_kwh
    from purchase
    left join {{ ref('fct_community_allocation') }} as actual using (metering_point, interval_start)

),

portfolio as (

    -- Readings that have not arrived do not count towards the direction, so while some are
    -- missing the price is chosen on part of the portfolio. portfolio_is_complete says so.
    select interval_start,
           sum(imbalance_kwh)          as portfolio_imbalance_kwh,
           count(*) = count(actual_kwh) as portfolio_is_complete
    from settled
    group by interval_start

)

select settled.metering_point,
       settled.interval_start,
       (settled.interval_start at time zone 'Europe/Vienna')::date as delivery_day,
       settled.forecast_method,
       settled.forecast_residual_kwh,
       settled.bought_kwh,
       settled.actual_kwh,
       settled.imbalance_kwh,
       settled.actual_kwh is not null                              as is_settled,
       portfolio.portfolio_is_complete,
       settled.day_ahead_price_eur_mwh,
       case when portfolio.portfolio_imbalance_kwh > 0 then imbalance_price.short_price_eur_mwh
            else imbalance_price.long_price_eur_mwh
       end                                                          as imbalance_price_eur_mwh,
       imbalance_price.imbalance_price_is_final,
       settled.bought_kwh / 1000 * settled.day_ahead_price_eur_mwh  as day_ahead_cost_eur,
       settled.imbalance_kwh / 1000
           * case when portfolio.portfolio_imbalance_kwh > 0 then imbalance_price.short_price_eur_mwh
                  else imbalance_price.long_price_eur_mwh
             end                                                    as imbalance_cost_eur
from settled
join portfolio using (interval_start)
left join imbalance_price using (interval_start)
