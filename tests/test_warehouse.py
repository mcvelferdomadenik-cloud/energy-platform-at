"""Tests for the warehouse writer that run without a database."""

from datetime import UTC, datetime, timedelta

import pytest

from megavolt.apcs import ProfilePoint
from megavolt.entsoe import PricePoint
from megavolt.warehouse import (
    WarehouseError,
    dsn,
    payload_hash,
    profile_payload_hash,
    store_day_ahead_prices,
    store_load_profiles,
)

ZONE = "10YAT-APG------L"
POINT = PricePoint(datetime(2026, 9, 11, 0, 0, tzinfo=UTC), 177.39)
PROFILE = ProfilePoint("H0", datetime(2025, 6, 21, 10, 30, tzinfo=UTC), 0.127)


def test_the_same_delivery_always_gets_the_same_fingerprint():
    assert payload_hash(ZONE, POINT) == payload_hash(ZONE, POINT)


def test_a_corrected_price_gets_a_different_fingerprint():
    corrected = PricePoint(POINT.interval_start, 165.00)
    assert payload_hash(ZONE, corrected) != payload_hash(ZONE, POINT)


def test_the_same_price_in_another_interval_gets_a_different_fingerprint():
    later = PricePoint(datetime(2026, 9, 11, 0, 15, tzinfo=UTC), POINT.price_eur_mwh)
    assert payload_hash(ZONE, later) != payload_hash(ZONE, POINT)


def test_missing_connection_string_explains_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_DSN", raising=False)
    with pytest.raises(WarehouseError, match=".env.example"):
        dsn()


def test_storing_nothing_is_refused_rather_than_quietly_accepted():
    with pytest.raises(WarehouseError, match="empty set"):
        store_day_ahead_prices([], ZONE, timedelta(minutes=15))


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
