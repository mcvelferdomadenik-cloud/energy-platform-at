-- One day-ahead price per quarter hour, whatever resolution the auction had. The Austrian day-ahead
-- auction priced whole hours until 30 September 2025 and quarter hours since, while meter readings
-- are always quarter-hourly. An hourly price applies to each of its four quarter hours; a
-- quarter-hourly price passes through unchanged. (The single day-ahead coupling moved to a
-- 15-minute market time unit on 1 October 2025; ENTSO-E publishes both under the same document.)
--
-- If the same quarter hour is covered twice, by an hour and by a quarter hour of its own, the
-- delivery we received last wins; among equals the finer one does.
--
-- A table, not a view: every forecast and purchase model joins on it, and as a view its
-- DISTINCT ON left the planner guessing (it estimated 3.5 billion rows for the purchase).

{{ config(
    materialized='table',
    indexes=[
        {'columns': ['bidding_zone', 'interval_start'], 'unique': true},
        {'columns': ['interval_start']},
    ]
) }}

select distinct on (price.bidding_zone, slot)
       price.bidding_zone,
       slot                 as interval_start,
       price.price_eur_mwh,
       price.resolution     as auction_resolution
from {{ ref('stg_day_ahead_price') }} as price
cross join lateral generate_series(
    price.interval_start,
    price.interval_start + price.resolution - interval '15 minutes',
    interval '15 minutes'
) as slot
order by price.bidding_zone, slot, price.received_at desc, price.resolution asc,
         price.interval_start desc
