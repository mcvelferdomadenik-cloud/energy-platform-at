-- One row per location and hour, temperature and global radiation side by side. The latest
-- delivery of each value wins. An hour the source never delivered is a null here, never a zero;
-- a value the source delivered once and later withdrew keeps its last delivered number.
-- Both values read as the state AT the hour, not as a mean over it: on 21 June 2025 radiation is
-- 0 at 03:00 UTC, five minutes before sunrise, and 0 again at 19:00, five minutes after sunset.
-- That is read from the data, not from documentation; confirm it before a forecast joins on it.
-- Source: GeoSphere Austria, INCA analysis, CC BY 4.0.

with latest as (

    select distinct on (dataset, latitude, longitude, parameter, valid_at)
           dataset,
           latitude,
           longitude,
           parameter,
           valid_at,
           value
    from {{ source('raw', 'weather_observation') }}
    order by dataset, latitude, longitude, parameter, valid_at,
             received_at desc, payload_hash desc

)

select dataset,
       latitude,
       longitude,
       valid_at,
       max(value) filter (where parameter = 'T2M') as temperature_c,
       max(value) filter (where parameter = 'GL')  as global_radiation_w_m2
from latest
group by dataset, latitude, longitude, valid_at
