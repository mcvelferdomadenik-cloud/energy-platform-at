-- Every Austrian day that has day-ahead prices at all must have one for each of its quarter hours:
-- 92, 96 or 100, depending on the clock change. Returns the days that do not.
--
-- This is the test that was missing when hourly prices were stored under a quarter-hour label and
-- covered only the first quarter of each hour: 24 priced quarter hours in a day of 96.
--
-- It looks at days that have some price. A day with no price at all is not an error here, because
-- prices exist only for the periods that were fetched; that a settled quarter hour has a price
-- is asserted where settlement joins them.

with per_day as (

    select bidding_zone,
           (interval_start at time zone 'Europe/Vienna')::date as delivery_day,
           count(*) as priced_quarter_hours
    from {{ ref('int_day_ahead_price_quarter_hour') }}
    group by bidding_zone, delivery_day

)

select *,
       (extract(epoch from (delivery_day + 1)::timestamp at time zone 'Europe/Vienna'
                         -  delivery_day::timestamp      at time zone 'Europe/Vienna')
        / 900)::integer as quarter_hours_in_the_day
from per_day
where priced_quarter_hours <> (extract(epoch from (delivery_day + 1)::timestamp at time zone 'Europe/Vienna'
                                               -  delivery_day::timestamp      at time zone 'Europe/Vienna')
                               / 900)::integer
