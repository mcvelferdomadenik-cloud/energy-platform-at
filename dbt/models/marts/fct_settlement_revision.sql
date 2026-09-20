-- Per customer and delivery day: what the settlement said on the morning after delivery, what it
-- says now, and why the two differ.
--
-- Only the readings differ between the two. The purchase was made the day before and does not
-- change, and both sides use the same imbalance price. Where a reading had arrived and was
-- replaced, the revision is (what they took according to today's reading - according to the next
-- morning's) x price. Where nothing had arrived, the imbalance was unknown, never zero, so the
-- whole imbalance cost of that quarter hour appears now: (what they took - what was bought) x price.
-- A revision of the imbalance PRICE hits every customer alike and is not a customer's story; it is
-- visible in stg_imbalance_price as intermediate versus final.
--
-- reason   late         nothing had arrived by the next morning, it has now
--          corrected    a reading that had arrived was replaced by a later version
--          completed    some quarter hours had arrived, more have since
--          unchanged    today's readings are the ones we had the next morning
--          missing      nothing has arrived to this day

with quarter_hour as (

    select cost.metering_point,
           cost.delivery_day,
           cost.bought_kwh,
           cost.imbalance_price_eur_mwh,
           cost.actual_kwh                                   as final_kwh,
           early.residual_kwh                                as preliminary_kwh,
           final_reading.version                             as final_version,
           early.version                                     as preliminary_version
    from {{ ref('fct_customer_cost') }} as cost
    left join {{ ref('int_meter_reading_preliminary') }} as early using (metering_point, interval_start)
    left join {{ ref('fct_community_allocation') }} as final_reading
           using (metering_point, interval_start)

),

customer_day as (

    select metering_point,
           delivery_day,
           count(preliminary_kwh)                                         as preliminary_quarter_hours,
           count(final_kwh)                                               as final_quarter_hours,
           -- A later version, or the same version sent again with another value.
           count(*) filter (where final_version > preliminary_version
                               or final_kwh <> preliminary_kwh)          as corrected_quarter_hours,
           -- A reading without an imbalance price has no cost on either side, so its revision
           -- would read as zero. It must be seen, not summed away.
           count(*) filter (where final_kwh is not null
                              and imbalance_price_eur_mwh is null)        as unpriced_quarter_hours,
           sum(preliminary_kwh)                                           as preliminary_kwh,
           sum(final_kwh)                                                 as final_kwh,
           sum((preliminary_kwh - bought_kwh) / 1000 * imbalance_price_eur_mwh)
                                                                          as preliminary_imbalance_cost_eur,
           sum((final_kwh - bought_kwh) / 1000 * imbalance_price_eur_mwh) as final_imbalance_cost_eur
    from quarter_hour
    group by metering_point, delivery_day

)

select *,
       coalesce(final_imbalance_cost_eur, 0) - coalesce(preliminary_imbalance_cost_eur, 0)
           as revision_eur,
       case
           when final_quarter_hours = 0                             then 'missing'
           when preliminary_quarter_hours = 0                       then 'late'
           when corrected_quarter_hours > 0                         then 'corrected'
           when final_quarter_hours > preliminary_quarter_hours     then 'completed'
           else 'unchanged'
       end as reason
from customer_day
