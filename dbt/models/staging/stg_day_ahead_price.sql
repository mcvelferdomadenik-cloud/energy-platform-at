-- One day-ahead price per bidding zone per interval. ENTSO-E has no version number, so the
-- delivery we received last wins.

select distinct on (bidding_zone, interval_start)
       bidding_zone,
       interval_start,
       resolution,
       price_eur_mwh,
       received_at
from {{ source('raw', 'day_ahead_price') }}
order by bidding_zone, interval_start, received_at desc, payload_hash desc
