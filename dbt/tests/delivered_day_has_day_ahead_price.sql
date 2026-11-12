-- A day we hold readings for must have its day-ahead prices. The purchase is an inner join on the
-- price, so a delivered day without one has no purchase and no cost row: it drops out of every
-- settlement mart while each number that remains still looks right. Returns the delivered days
-- with fewer priced quarter hours than the shortest day has (92, the spring clock change); that a
-- priced day has exactly its 92, 96 or 100 is asserted in day_ahead_price_covers_every_quarter_hour.
-- Error: a day without a price is a day without a cost. The fix is to run entsoe_day_ahead_prices
-- for the day BEFORE the one returned (each run fetches the next delivery day), then build again.
--
-- No bound on arrival time, unlike allocation_is_current: a day is auctioned the day before
-- delivery and its readings arrive the day after, so a missing price is never this build's timing.

with delivered as (

    select distinct delivery_day
    from {{ ref('fct_customer_day') }}

),

priced as (

    select (interval_start at time zone 'Europe/Vienna')::date as delivery_day,
           count(*) as priced_quarter_hours
    from {{ ref('int_day_ahead_price_quarter_hour') }}
    where bidding_zone = '10YAT-APG------L'
    group by 1

)

select delivered.delivery_day,
       coalesce(priced.priced_quarter_hours, 0) as priced_quarter_hours
from delivered
left join priced using (delivery_day)
where coalesce(priced.priced_quarter_hours, 0) < 92
