-- The allocation mart is incremental and only reworks a window of recent days. The one way such
-- a model goes wrong is quietly: a reading or correction that lands outside the window is never
-- built, and every mart downstream agrees with the hole because they are built from it. The
-- staging view is always current, so a row it has and the mart lacks is that hole. Error, not
-- warn: a missing row is missing money. The fix is `dbt build --full-refresh` for the models
-- that read readings (see dbt/macros/recent_only.sql).

select reading.metering_point, reading.interval_start, reading.version, reading.delivered_at
from {{ ref('stg_meter_reading') }} as reading
left join {{ ref('fct_community_allocation') }} as allocation
  on allocation.metering_point = reading.metering_point
 and allocation.interval_start = reading.interval_start
 and allocation.version = reading.version
 and allocation.delivered_at = reading.delivered_at
where allocation.metering_point is null
