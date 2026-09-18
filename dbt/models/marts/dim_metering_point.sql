-- Our customers as registered today. Pseudonymous by construction: an identifier, a segment,
-- a shape and a size. No name and no address exists anywhere in the platform.

select metering_point,
       profile_type,
       segment,
       annual_kwh,
       meter_id,
       valid_from as registered_since
from {{ ref('stg_metering_point') }}
where valid_to is null
