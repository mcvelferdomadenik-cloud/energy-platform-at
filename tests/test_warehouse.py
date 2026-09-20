"""Tests for the warehouse writer that run without a database."""

from datetime import UTC, datetime, timedelta

import pytest

from megavolt.apcs import ProfilePoint
from megavolt.community import VALID_FROM, members
from megavolt.entsoe import ImbalancePricePoint, LoadPoint, PricePoint
from megavolt.readings import CommunityReading, Reading
from megavolt.warehouse import (
    WarehouseError,
    community_payload_hash,
    dsn,
    imbalance_payload_hash,
    load_payload_hash,
    payload_hash,
    profile_payload_hash,
    reading_payload_hash,
    store_actual_load,
    store_day_ahead_prices,
    store_imbalance_prices,
    store_load_profiles,
    store_metering_points,
    store_stream_batch,
)

ZONE = "10YAT-APG------L"
QUARTER = timedelta(minutes=15)
POINT = PricePoint(datetime(2026, 9, 11, 0, 0, tzinfo=UTC), 177.39, QUARTER)
PROFILE = ProfilePoint("H0", datetime(2025, 6, 21, 10, 30, tzinfo=UTC), 0.127)
READING = Reading(
    "AT09999908430BUZOSQIZOKT57LP7GYW2",
    datetime(2025, 3, 1, 23, 0, tzinfo=UTC),
    0.142,
    0.05,
    "MV-000042-A",
    1,
    datetime(2025, 3, 3, 2, 30, tzinfo=UTC),
)
INTERVAL = CommunityReading(
    datetime(2025, 3, 1, 23, 0, tzinfo=UTC), 12.5, 130.0, 1, datetime(2025, 3, 3, 2, 30, tzinfo=UTC)
)


def test_the_same_delivery_always_gets_the_same_fingerprint():
    assert payload_hash(ZONE, POINT) == payload_hash(ZONE, POINT)


def test_a_corrected_price_gets_a_different_fingerprint():
    corrected = PricePoint(POINT.interval_start, 165.00, QUARTER)
    assert payload_hash(ZONE, corrected) != payload_hash(ZONE, POINT)


def test_the_same_price_in_another_interval_gets_a_different_fingerprint():
    later = PricePoint(datetime(2026, 9, 11, 0, 15, tzinfo=UTC), POINT.price_eur_mwh, QUARTER)
    assert payload_hash(ZONE, later) != payload_hash(ZONE, POINT)


def test_missing_connection_string_explains_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_DSN", raising=False)
    with pytest.raises(WarehouseError, match=".env.example"):
        dsn()


def test_storing_nothing_is_refused_rather_than_quietly_accepted():
    with pytest.raises(WarehouseError, match="empty set"):
        store_day_ahead_prices([], ZONE)


def test_the_same_profile_value_always_gets_the_same_fingerprint():
    assert profile_payload_hash(PROFILE, 2025) == profile_payload_hash(PROFILE, 2025)


def test_the_same_interval_in_another_profile_year_gets_a_different_fingerprint():
    assert profile_payload_hash(PROFILE, 2024) != profile_payload_hash(PROFILE, 2025)


def test_a_revised_profile_value_gets_a_different_fingerprint():
    revised = ProfilePoint(PROFILE.profile_type, PROFILE.interval_start, 0.131)
    assert profile_payload_hash(revised, 2025) != profile_payload_hash(PROFILE, 2025)


def test_storing_no_profiles_is_refused_before_the_database_is_touched():
    with pytest.raises(WarehouseError, match="empty set"):
        store_load_profiles(iter([]), 2025)


def test_storing_no_metering_points_is_refused():
    with pytest.raises(WarehouseError, match="empty set"):
        store_metering_points([], VALID_FROM)


def test_a_metering_point_that_is_not_our_customer_is_refused():
    theirs = [member for member in members("test-seed") if not member.ours]
    with pytest.raises(WarehouseError, match="not our customers"):
        store_metering_points(theirs[:1], VALID_FROM)


def test_our_own_metering_points_are_accepted_up_to_the_database_call(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_DSN", raising=False)
    ours = [member for member in members("test-seed") if member.ours]
    with pytest.raises(WarehouseError, match=".env.example"):
        store_metering_points(ours[:1], VALID_FROM)


# --- the meter stream -------------------------------------------------------------------------


def test_a_replayed_reading_gets_the_same_fingerprint_so_it_inserts_nothing():
    replay = Reading(*(getattr(READING, field) for field in Reading.__slots__))
    assert reading_payload_hash(replay) == reading_payload_hash(READING)


def test_a_corrected_reading_gets_a_different_fingerprint_so_both_versions_are_kept():
    corrected = Reading(
        READING.metering_point,
        READING.interval_start,
        0.180,
        READING.allocated_kwh,
        READING.meter_id,
        2,
        datetime(2025, 3, 10, 2, 30, tzinfo=UTC),
    )
    assert reading_payload_hash(corrected) != reading_payload_hash(READING)


def test_the_same_values_under_a_new_meter_are_a_different_delivery():
    swapped = Reading(
        READING.metering_point,
        READING.interval_start,
        READING.consumption_kwh,
        READING.allocated_kwh,
        "MV-000042-B",
        READING.version,
        READING.delivered_at,
    )
    assert reading_payload_hash(swapped) != reading_payload_hash(READING)


def test_a_replayed_community_interval_gets_the_same_fingerprint():
    replay = CommunityReading(*(getattr(INTERVAL, field) for field in CommunityReading.__slots__))
    assert community_payload_hash(replay) == community_payload_hash(INTERVAL)


def test_a_corrected_community_interval_gets_a_different_fingerprint():
    corrected = CommunityReading(
        INTERVAL.interval_start, 14.0, INTERVAL.consumption_kwh, 2, INTERVAL.delivered_at
    )
    assert community_payload_hash(corrected) != community_payload_hash(INTERVAL)


def test_an_empty_stream_batch_is_refused_before_the_database_is_touched(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_DSN", raising=False)
    with pytest.raises(WarehouseError, match="empty batch"):
        store_stream_batch([], [])


# --- imbalance prices and actual load ----------------------------------------------------------

IMBALANCE = ImbalancePricePoint(
    datetime(2025, 3, 30, 23, 0, tzinfo=UTC), "A04", 103.75, "A01", QUARTER
)
LOAD = LoadPoint(datetime(2025, 3, 30, 23, 0, tzinfo=UTC), 5429.2, QUARTER)


def test_an_imbalance_price_that_becomes_final_is_a_new_delivery():
    final = ImbalancePricePoint(IMBALANCE.interval_start, "A04", 103.75, "A02", QUARTER)
    assert imbalance_payload_hash(ZONE, final) != imbalance_payload_hash(ZONE, IMBALANCE)


def test_the_two_directions_of_one_interval_never_collide():
    short = ImbalancePricePoint(IMBALANCE.interval_start, "A05", 103.75, "A01", QUARTER)
    assert imbalance_payload_hash(ZONE, short) != imbalance_payload_hash(ZONE, IMBALANCE)


def test_a_re_fetched_unchanged_imbalance_price_inserts_nothing():
    again = ImbalancePricePoint(IMBALANCE.interval_start, "A04", 103.75, "A01", QUARTER)
    assert imbalance_payload_hash(ZONE, again) == imbalance_payload_hash(ZONE, IMBALANCE)


def test_a_revised_load_value_is_a_new_delivery_and_a_repeat_is_not():
    assert load_payload_hash(
        ZONE, LoadPoint(LOAD.interval_start, 5429.2, QUARTER)
    ) == load_payload_hash(ZONE, LOAD)
    assert load_payload_hash(
        ZONE, LoadPoint(LOAD.interval_start, 5430.0, QUARTER)
    ) != load_payload_hash(ZONE, LOAD)


def test_storing_no_imbalance_prices_or_load_is_refused():
    with pytest.raises(WarehouseError, match="empty set"):
        store_imbalance_prices([], ZONE)
    with pytest.raises(WarehouseError, match="empty set"):
        store_actual_load([], ZONE)


def test_the_same_price_for_an_hour_and_for_a_quarter_hour_are_different_deliveries():
    hourly = PricePoint(POINT.interval_start, POINT.price_eur_mwh, timedelta(hours=1))
    assert payload_hash(ZONE, hourly) != payload_hash(ZONE, POINT)
