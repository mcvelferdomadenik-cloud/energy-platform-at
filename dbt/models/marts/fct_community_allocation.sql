-- One row per customer and quarter hour: what they consumed, what the community covered, and what
-- is left for us to supply. The allocation itself ARRIVES with the reading, computed by the grid
-- operator; we receive only our own customers and the community totals, so this model does not
-- compute the allocation, it checks it against the rule the community uses:
--
--     allocated = consumption x min(1, community generation / community consumption)
--
-- Source: the dynamic allocation model for Austrian energy communities, energiegemeinschaften.gv.at.
--
-- A reading that does not follow the rule is almost always a first delivery that a correction will
-- replace within days. The flag only sees what the rule can see: at night, when the community covers
-- nothing, a wrong consumption still follows it, so a true flag is not proof that a reading is right.

{{ config(indexes=[
    {'columns': ['metering_point', 'interval_start'], 'unique': true},
    {'columns': ['interval_start']},
]) }}

with reading as (

    select * from {{ ref('stg_meter_reading') }}

),

community as (

    select interval_start,
           generation_kwh,
           consumption_kwh,
           -- A community that consumes nothing has nothing to cover; every member's share of zero is
           -- zero whatever the ratio, so it is set to 1 rather than left undefined.
           case when consumption_kwh = 0 then 1
                else least(1, generation_kwh / consumption_kwh)
           end as coverage_ratio
    from {{ ref('stg_community_interval') }}

)

select reading.metering_point,
       reading.interval_start,
       (reading.interval_start at time zone 'Europe/Vienna')::date   as delivery_day,
       reading.profile_type,
       reading.segment,
       reading.consumption_kwh,
       reading.allocated_kwh,
       reading.consumption_kwh - reading.allocated_kwh               as residual_kwh,
       community.coverage_ratio,
       reading.consumption_kwh * community.coverage_ratio            as expected_allocated_kwh,
       -- Readings are rounded to four decimals and the rule is not, so an honest reading can be off
       -- by half of the last digit on each side.
       abs(reading.allocated_kwh - reading.consumption_kwh * community.coverage_ratio) <= 0.0002
                                                                     as follows_allocation_rule,
       reading.version,
       reading.delivered_at
from reading
-- Left join: a reading whose quarter hour has no community totals yet is kept, with nulls. It is
-- missing information, and it must look missing rather than disappear.
left join community using (interval_start)
