-- One imbalance price per control area, interval and direction. A final document (A02) beats an
-- intermediate one (A01); among equals the latest delivery wins.

select distinct on (control_area, interval_start, category)
       control_area,
       interval_start,
       category,
       case category when 'A04' then 'long' when 'A05' then 'short' end as direction,
       price_eur_mwh,
       doc_status,
       doc_status = 'A02' as is_final,
       received_at
from {{ source('raw', 'imbalance_price') }}
order by control_area, interval_start, category,
         doc_status desc, received_at desc, payload_hash desc
