-- What the community reported about itself, one row per quarter hour, latest version winning.

select distinct on (interval_start)
       interval_start,
       generation_kwh,
       consumption_kwh,
       version,
       delivered_at
from {{ source('raw', 'community_interval') }}
order by interval_start, version desc, delivered_at desc, payload_hash desc
