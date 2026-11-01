-- The allocation mart is incremental and only reworks a window of recent days. The one way such
-- a model goes wrong is quietly: a reading or correction that lands outside the window is never
-- built, and every mart downstream agrees with the hole because they are built from it. The
-- staging view is always current, so a row it has and the mart lacks is that hole. Error, not
-- warn: a missing row is missing money. The fix is `dbt build --full-refresh` for the models
-- that read readings (see dbt/macros/recent_only.sql).
--
-- Only readings stored a minute before this build began: the build starts the moment a delivery
-- is stored, and the next delivery of a catch-up can land while it runs. Those rows are the next
-- build's job. The minute covers a batch whose transaction began before the build and committed
-- after the mart's own read (received_at is the transaction's start). One clock: the database and
-- dbt share the host's.

select reading.metering_point, reading.interval_start, reading.version, reading.delivered_at
from {{ ref('stg_meter_reading') }} as reading
left join {{ ref('fct_community_allocation') }} as allocation
  on allocation.metering_point = reading.metering_point
 and allocation.interval_start = reading.interval_start
 and allocation.version = reading.version
 and allocation.delivered_at = reading.delivered_at
where allocation.metering_point is null
  and reading.received_at < '{{ run_started_at }}'::timestamptz - interval '1 minute'
