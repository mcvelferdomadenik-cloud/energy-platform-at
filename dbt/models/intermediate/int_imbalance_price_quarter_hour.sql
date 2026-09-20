-- The two imbalance prices of a quarter hour side by side: what we are paid for a surplus (long)
-- and what we pay for a shortfall (short). Austria has priced them alike in every day seen so far,
-- but the rules allow them to differ, so which one applies is decided where the direction is known.

select interval_start,
       max(price_eur_mwh) filter (where direction = 'long')  as long_price_eur_mwh,
       max(price_eur_mwh) filter (where direction = 'short') as short_price_eur_mwh,
       bool_and(is_final)                                    as imbalance_price_is_final
from {{ ref('stg_imbalance_price') }}
group by interval_start
