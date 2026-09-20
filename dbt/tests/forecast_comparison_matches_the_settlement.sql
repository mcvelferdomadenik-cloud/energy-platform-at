-- The comparison of methods and the settlement are two roads to the same numbers for the method we
-- operate on. Returns the quarter hours where they differ.
--
-- fct_forecast_comparison sums the portfolio straight from the purchases; fct_supplier_settlement
-- sums what was attributed to each customer. If the two disagree, one of them lost or doubled a
-- customer, or picked another price.

select comparison.interval_start,
       comparison.imbalance_cost_eur  as compared_imbalance_cost_eur,
       settlement.imbalance_cost_eur  as settled_imbalance_cost_eur,
       comparison.day_ahead_cost_eur  as compared_day_ahead_cost_eur,
       settlement.day_ahead_cost_eur  as settled_day_ahead_cost_eur
-- Filtered BEFORE the join: a filter on the comparison's side afterwards would turn the full join
-- into a left join and hide a quarter hour that the settlement has and the comparison lacks.
from (
    select * from {{ ref('fct_forecast_comparison') }} where forecast_method = 'standard_profile'
) as comparison
full join {{ ref('fct_supplier_settlement') }} as settlement using (interval_start)
where (abs(coalesce(comparison.imbalance_cost_eur, 0) - coalesce(settlement.imbalance_cost_eur, 0)) > 1e-6
       or abs(coalesce(comparison.day_ahead_cost_eur, 0) - coalesce(settlement.day_ahead_cost_eur, 0)) > 1e-6
       or abs(coalesce(comparison.perfect_forecast_cost_eur, 0)
              - coalesce(settlement.perfect_forecast_cost_eur, 0)) > 1e-6)
