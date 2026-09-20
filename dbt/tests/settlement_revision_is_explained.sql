-- Every euro by which a settlement changed must have a reason, and a customer-day called unchanged
-- must not have moved. Returns the customer-days that break either rule.
--
-- The readings of the next morning are a subset of today's, so today can never know less.

select *
from {{ ref('fct_settlement_revision') }}
where (reason = 'unchanged' and abs(revision_eur) > 1e-9)
   or (reason = 'unchanged' and abs(coalesce(final_kwh, 0) - coalesce(preliminary_kwh, 0)) > 1e-9)
   or (reason = 'missing'   and (preliminary_quarter_hours > 0 or abs(revision_eur) > 1e-9))
   -- a reading we cannot price would show a revision of zero whatever happened to it
   or unpriced_quarter_hours > 0
   or final_quarter_hours < preliminary_quarter_hours
