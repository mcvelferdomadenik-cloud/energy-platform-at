-- The official APCS shapes, one value per profile per interval, latest download winning.

select distinct on (profile_type, interval_start)
       profile_type,
       interval_start,
       profile_year,
       value
from {{ source('raw', 'load_profile') }}
order by profile_type, interval_start, received_at desc, payload_hash desc
