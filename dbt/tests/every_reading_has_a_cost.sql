-- Energy we delivered in a priced quarter hour must appear in the cost. Returns the readings that
-- do not.
--
-- The forecast is built with inner joins: a customer whose profile class has no value for that
-- quarter hour, or a quarter hour without the photovoltaic profile, has no forecast, so no cost
-- row, and the energy they took would vanish from the books without any number looking wrong.

select allocation.metering_point,
       allocation.interval_start,
       allocation.residual_kwh
from {{ ref('fct_community_allocation') }} as allocation
join (select distinct interval_start from {{ ref('int_day_ahead_price_quarter_hour') }}) as priced
  using (interval_start)
left join {{ ref('fct_customer_cost') }} as cost using (metering_point, interval_start)
where cost.metering_point is null
