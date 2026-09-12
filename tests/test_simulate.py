"""Tests for the truth model. No database, no broker, no network.

The profiles are synthetic here on purpose: these tests are about the arithmetic and the
determinism, not about the APCS file, which `test_apcs.py` already covers.
"""

from datetime import date, datetime, timedelta

import pytest

from megavolt.community import GENERATION_PROFILE, GENERATION_SEGMENT, VALID_FROM, VIENNA, Member
from megavolt.readings import decode, encode, readings_of, utc_text
from megavolt.simulate import (
    COMMUNITY_KEY,
    LATE_DAYS,
    MAX_LOOKBACK_DAYS,
    METER_LETTERS,
    SimulationError,
    _correction,
    _delivered_at,
    _first_delivery,
    allocate,
    community_totals,
    consumption_series,
    correction_lag_days,
    day_intervals,
    delay_days,
    deliveries_for,
    generation_series,
    is_corrected,
    is_missing,
    meter_history,
    meter_id_on,
    registrations,
    rng,
    simulate_day,
    surplus,
)

SEED = "test-seed"
DAY = date(2025, 3, 1)
INTERVALS = day_intervals(DAY)

HOUSE = Member("AT09999908430AAAAAAAAAAAAAAAAAAAA", "H0", "household", 3500.0, "MV-000001-A", True)
SHOP = Member("AT09999908430BBBBBBBBBBBBBBBBBBBB", "H0", "business", 7000.0, "MV-000002-A", False)
PLANT = Member(
    "AT09999908430CCCCCCCCCCCCCCCCCCCC",
    GENERATION_PROFILE,
    GENERATION_SEGMENT,
    2000.0,
    "MV-000003-A",
    False,
)


def profiles(house=0.03, plant=0.05):
    """A flat stand-in for the real shapes: every interval carries the same value."""
    return {
        "H0": dict.fromkeys(INTERVALS, house),
        GENERATION_PROFILE: dict.fromkeys(INTERVALS, plant),
    }


# --- the day, and the two days a year that are not 24 hours long -----------------------------


def test_an_ordinary_austrian_day_is_ninety_six_intervals():
    assert len(day_intervals(date(2025, 3, 1))) == 96


def test_the_spring_clock_change_makes_a_ninety_two_interval_day():
    assert len(day_intervals(date(2025, 3, 30))) == 92


def test_the_autumn_clock_change_makes_a_hundred_interval_day():
    assert len(day_intervals(date(2025, 10, 26))) == 100


def test_the_day_starts_at_austrian_local_midnight_expressed_in_utc():
    assert day_intervals(date(2025, 3, 1))[0] == datetime.fromisoformat("2025-02-28T23:00:00+00:00")
    assert day_intervals(date(2025, 7, 1))[0] == datetime.fromisoformat("2025-06-30T22:00:00+00:00")


def test_each_interval_follows_the_previous_one_by_a_quarter_hour():
    intervals = day_intervals(date(2025, 3, 30))
    gaps = {later - earlier for earlier, later in zip(intervals, intervals[1:], strict=False)}
    assert gaps == {timedelta(minutes=15)}


# --- determinism: the property the replay guarantee rests on ---------------------------------


def test_the_same_seed_and_day_always_draw_the_same_numbers():
    assert rng(SEED, "x", DAY).random() == rng(SEED, "x", DAY).random()


def test_a_different_day_draws_different_numbers():
    assert rng(SEED, "x", DAY).random() != rng(SEED, "x", date(2025, 3, 2)).random()


def test_the_same_member_on_the_same_day_always_consumes_the_same():
    assert consumption_series(HOUSE, INTERVALS, profiles(), SEED) == consumption_series(
        HOUSE, INTERVALS, profiles(), SEED
    )


def test_another_seed_produces_another_day():
    assert consumption_series(HOUSE, INTERVALS, profiles(), SEED) != consumption_series(
        HOUSE, INTERVALS, profiles(), "other-seed"
    )


def test_two_members_on_the_same_profile_do_not_move_in_lockstep():
    one = consumption_series(HOUSE, INTERVALS, profiles(), SEED)
    other = consumption_series(SHOP, INTERVALS, profiles(), SEED)
    assert [value * 2 for value in one] != list(other)


def test_consumption_scales_with_the_annual_figure_and_nothing_else():
    bigger = Member(HOUSE.metering_point, "H0", "household", 7000.0, "MV-000001-A", True)
    one = consumption_series(HOUSE, INTERVALS, profiles(), SEED)
    twice = consumption_series(bigger, INTERVALS, profiles(), SEED)
    # Tolerance covers both roundings: round(2x) and 2*round(x) can differ by 1.5 units.
    for small, large in zip(one, twice, strict=True):
        assert large == pytest.approx(2 * small, abs=2e-4)


# --- what the simulator refuses to invent ----------------------------------------------------


def test_a_profile_that_was_never_loaded_is_refused():
    with pytest.raises(SimulationError, match="no G3 profile was loaded"):
        consumption_series(
            Member(HOUSE.metering_point, "G3", "business", 100.0, "MV-000001-A", True),
            INTERVALS,
            profiles(),
            SEED,
        )


def test_a_day_the_profile_does_not_cover_is_refused_rather_than_filled_in():
    with pytest.raises(SimulationError, match="has no value for"):
        consumption_series(HOUSE, day_intervals(date(2026, 3, 1)), profiles(), SEED)


def test_a_consuming_member_cannot_be_used_as_the_plant():
    with pytest.raises(SimulationError, match="is not the generation point"):
        generation_series(HOUSE, INTERVALS, profiles(), SEED)


def test_a_registry_without_exactly_one_plant_is_refused():
    with pytest.raises(SimulationError, match="exactly one generation point"):
        simulate_day(DAY, (HOUSE, SHOP), profiles(), SEED)


# --- allocation: the community's step, before ours --------------------------------------------


def flat(*values):
    """One member per value, each consuming that amount in every interval."""
    return {f"point-{index}": (value,) for index, value in enumerate(values)}


def test_nobody_is_allocated_more_than_they_consumed():
    allocated = allocate(flat(1.0, 3.0), (100.0,))
    assert allocated["point-0"] == (1.0,)
    assert allocated["point-1"] == (3.0,)


def test_with_no_generation_nobody_is_allocated_anything():
    assert allocate(flat(1.0, 3.0), (0.0,)) == {"point-0": (0.0,), "point-1": (0.0,)}


def test_generation_is_shared_in_proportion_to_consumption():
    allocated = allocate(flat(1.0, 3.0), (2.0,))
    assert allocated["point-0"] == pytest.approx((0.5,))
    assert allocated["point-1"] == pytest.approx((1.5,))


def test_an_interval_nobody_consumed_in_allocates_nothing():
    assert allocate(flat(0.0, 0.0), (5.0,)) == {"point-0": (0.0,), "point-1": (0.0,)}


def test_surplus_is_what_the_community_could_not_use():
    assert surplus((10.0,), flat(1.0, 3.0)) == (6.0,)
    assert surplus((2.0,), flat(1.0, 3.0)) == (0.0,)


def test_allocation_plus_surplus_equals_generation():
    """The invariant the whole settlement rests on: no energy appears and none disappears."""
    consumption = {
        member.metering_point: consumption_series(member, INTERVALS, profiles(), SEED)
        for member in (HOUSE, SHOP)
    }
    generation = generation_series(PLANT, INTERVALS, profiles(), SEED)
    allocated = allocate(consumption, generation)
    spare = surplus(generation, consumption)

    for position, made in enumerate(generation):
        shared = sum(series[position] for series in allocated.values())
        assert shared + spare[position] == pytest.approx(made, abs=1e-3)


def test_a_full_day_returns_an_allocation_for_every_consuming_member():
    intervals, consumption, allocated, generation = simulate_day(
        DAY, (HOUSE, SHOP, PLANT), profiles(), SEED
    )
    assert len(intervals) == 96
    assert set(consumption) == set(allocated) == {HOUSE.metering_point, SHOP.metering_point}
    assert PLANT.metering_point not in consumption
    assert len(generation) == 96


def test_the_community_total_is_the_sum_of_its_members():
    assert community_totals(flat(1.0, 3.0)) == (4.0,)


# --- delivery: late, corrected, incomplete ---------------------------------------------------

REGISTRY = (HOUSE, SHOP, PLANT)
RUN_DAY = date(2025, 3, 5)


def wide_profiles():
    """Flat profiles covering every day a run can reach back into."""
    days = [RUN_DAY - timedelta(days=back) for back in range(MAX_LOOKBACK_DAYS + 2)]
    intervals = [interval for day in days for interval in day_intervals(day)]
    return {
        "H0": dict.fromkeys(intervals, 0.03),
        GENERATION_PROFILE: dict.fromkeys(intervals, 0.05),
    }


def test_a_run_delivers_the_same_messages_every_time():
    one = deliveries_for(RUN_DAY, REGISTRY, wide_profiles(), SEED)
    other = deliveries_for(RUN_DAY, REGISTRY, wide_profiles(), SEED)
    assert [encode(d.payload) for d in one] == [encode(d.payload) for d in other]


def test_a_run_always_carries_the_community_aggregate_for_yesterday():
    deliveries = deliveries_for(RUN_DAY, REGISTRY, wide_profiles(), SEED)
    community = [d for d in deliveries if d.key == COMMUNITY_KEY]
    assert len(community) == 1
    assert community[0].payload["interval_start"] == utc_text(
        day_intervals(RUN_DAY - timedelta(days=1))[0]
    )


def test_a_run_never_carries_a_member_who_is_not_ours():
    deliveries = deliveries_for(RUN_DAY, REGISTRY, wide_profiles(), SEED)
    assert SHOP.metering_point not in {d.key for d in deliveries}
    assert PLANT.metering_point not in {d.key for d in deliveries}


def test_a_registry_with_none_of_our_customers_is_refused():
    with pytest.raises(SimulationError, match="none of our customers"):
        deliveries_for(RUN_DAY, (SHOP, PLANT), wide_profiles(), SEED)


def test_every_message_a_run_produces_passes_the_validation_it_will_meet():
    for delivery in deliveries_for(RUN_DAY, REGISTRY, wide_profiles(), SEED):
        assert readings_of(decode(encode(delivery.payload)))


def test_a_delivery_lands_the_morning_after_the_consumption_day_plus_its_delay():
    point, day = HOUSE.metering_point, date(2025, 3, 4)
    landing = _delivered_at(day, delay_days(point, day, SEED))
    assert landing.astimezone(VIENNA).hour == 3
    assert landing.astimezone(VIENNA).minute == 30
    assert landing.astimezone(VIENNA).date() == day + timedelta(
        days=1 + delay_days(point, day, SEED)
    )


def test_the_delay_of_a_point_day_never_depends_on_when_it_is_asked():
    point, day = HOUSE.metering_point, date(2025, 3, 4)
    assert delay_days(point, day, SEED) == delay_days(point, day, SEED)
    assert 0 <= delay_days(point, day, SEED) <= max(LATE_DAYS)


def test_a_correction_carries_a_higher_version_and_leaves_the_rest_untouched():
    point, day = _a_corrected_point_day()
    intervals, consumption, allocated, _ = simulate_day(day, REGISTRY, wide_profiles(), SEED)
    correction = _correction(HOUSE, day, intervals, consumption, allocated, SEED)

    assert correction is not None
    assert correction.payload["version"] == 2
    touched = [value for value in correction.payload["consumption"] if value is not None]
    assert 0 < len(touched) < len(intervals)


def test_a_correction_disagrees_with_the_first_delivery_it_replaces():
    point, day = _a_corrected_point_day()
    intervals, consumption, allocated, _ = simulate_day(day, REGISTRY, wide_profiles(), SEED)
    first = _first_delivery(HOUSE, day, intervals, consumption, allocated, SEED)
    correction = _correction(HOUSE, day, intervals, consumption, allocated, SEED)

    differences = [
        position
        for position, value in enumerate(correction.payload["consumption"])
        if value is not None and value != first.payload["consumption"][position]
    ]
    assert differences, "a correction that changes nothing is not a correction"


def test_a_correction_arrives_after_the_delivery_it_corrects():
    point, day = _a_corrected_point_day()
    first = _delivered_at(day, delay_days(point, day, SEED))
    later = _delivered_at(day, delay_days(point, day, SEED) + correction_lag_days(point, day, SEED))
    assert later > first


def _a_corrected_point_day():
    """Find a day the household's first delivery is wrong on, so the case can be tested."""
    point = HOUSE.metering_point
    for back in range(400):
        day = RUN_DAY - timedelta(days=back)
        if is_corrected(point, day, SEED):
            return point, day
    raise AssertionError("no corrected point-day found, which makes the rate implausible")


def test_an_absent_interval_is_left_absent_rather_than_zeroed():
    day = date(2025, 3, 4)
    intervals, consumption, allocated, _ = simulate_day(day, REGISTRY, wide_profiles(), SEED)
    first = _first_delivery(HOUSE, day, intervals, consumption, allocated, SEED)
    for position, value in enumerate(first.payload["consumption"]):
        expected_absent = is_missing(HOUSE.metering_point, day, position, SEED)
        assert (value is None) == expected_absent


# --- meters -----------------------------------------------------------------------------------


def test_a_meter_identifier_keeps_its_point_and_only_changes_its_letter():
    served = meter_id_on(HOUSE, date(2025, 6, 1), SEED)
    assert served.startswith(HOUSE.meter_id[:-1])
    assert served[-1] in METER_LETTERS


def test_the_meter_history_starts_on_the_day_the_point_was_registered():
    history = meter_history(HOUSE, 2025, SEED)
    assert history[0][0] == VALID_FROM.date()
    assert [entry[0] for entry in history] == sorted(entry[0] for entry in history)


def test_a_meter_that_was_never_exchanged_has_one_entry():
    never = [member for member in (HOUSE, SHOP) if len(meter_history(member, 2025, SEED)) == 1]
    assert never, "with a 2% yearly rate at least one of two points keeps its meter"


def test_registrations_only_ever_cover_our_own_points():
    grouped = registrations(REGISTRY, 2025, SEED)
    registered = {member.metering_point for group in grouped.values() for member in group}
    assert registered == {HOUSE.metering_point}
