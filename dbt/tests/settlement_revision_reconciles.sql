-- Both sides of the revision must be what the models they come from say. Returns the delivery days
-- where either is not.
--
--   final        the imbalance cost summed over customer-days is the one the portfolio mart reports
--                by quarter hour
--   preliminary  the energy summed over customer-days is the energy in the readings of the next
--                morning, for the quarter hours that have a price
-- A customer-day doubled or dropped on the way, or a next-morning reading that finds no cost row
-- to attach to, makes the sides part.

with by_customer as (

    select delivery_day,
           sum(final_imbalance_cost_eur) as imbalance_cost_eur,
           sum(preliminary_kwh)          as preliminary_kwh
    from {{ ref('fct_settlement_revision') }}
    group by delivery_day

),

by_quarter_hour as (

    select delivery_day,
           sum(imbalance_cost_eur) as imbalance_cost_eur
    from {{ ref('fct_supplier_settlement') }}
    group by delivery_day

),

next_morning as (

    select (early.interval_start at time zone 'Europe/Vienna')::date as delivery_day,
           sum(early.residual_kwh) as preliminary_kwh
    from {{ ref('int_meter_reading_preliminary') }} as early
    join (select distinct interval_start from {{ ref('int_day_ahead_price_quarter_hour') }}) as priced
      using (interval_start)
    group by 1

)

select *
from by_customer
full join by_quarter_hour using (delivery_day)
full join next_morning using (delivery_day)
where abs(coalesce(by_customer.imbalance_cost_eur, 0)
          - coalesce(by_quarter_hour.imbalance_cost_eur, 0)) > 1e-6
   or abs(coalesce(by_customer.preliminary_kwh, 0)
          - coalesce(next_morning.preliminary_kwh, 0)) > 1e-6
