-- Energy must not appear or disappear. Returns the quarter hours where it does.
--
--   1. what the community shared plus what it sent back is exactly what it generated
--   2. our customers cannot have consumed more than the whole community
--   3. our customers cannot have been allocated more than the community shared
--   4. what we supplied is what they consumed minus what they were allocated
--
-- Rules 1 to 3 compare against community totals. Where those are missing the comparison is NULL and
-- returns no row, so a quarter hour without totals passes here by saying nothing; that case has
-- its own test, supplier_position_has_community_totals.
--
-- The tolerance on 2 and 3 covers rounding of a few hundred readings to four decimals; 1 and 4 are
-- arithmetic on the same numbers and get a tenth of a watt hour, which a sum of doubles never
-- reaches however large the portfolio grows.

select *
from {{ ref('fct_supplier_position') }}
where abs(community_allocated_kwh + community_surplus_kwh - community_generation_kwh) > 1e-4
   or our_consumption_kwh > community_consumption_kwh + 0.02
   or our_allocated_kwh   > community_allocated_kwh   + 0.02
   or abs(our_consumption_kwh - our_allocated_kwh - our_residual_kwh) > 1e-4
