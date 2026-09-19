-- One load value per bidding zone and interval, in MW; the latest delivery wins.

select distinct on (bidding_zone, interval_start)
       bidding_zone,
       interval_start,
       resolution,
       load_mw,
       received_at
from {{ source('raw', 'actual_load') }}
order by bidding_zone, interval_start, received_at desc, payload_hash desc
