-- Quarter hours where our customers reported but the community did not. Returns them.
--
-- warn, not error. The community reports each day once; a day that was never delivered stays
-- missing for good, while late readings for it keep arriving. Our own volume does not depend on
-- the totals, only the check of the allocation rule does, so this must be visible and counted
-- without stopping the build for a gap that will never close.

{{ config(severity='warn') }}

select interval_start,
       delivery_day,
       customers_reporting
from {{ ref('fct_supplier_position') }}
where community_generation_kwh is null
