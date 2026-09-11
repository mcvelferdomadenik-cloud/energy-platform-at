"""Tests for the warehouse writer that run without a database."""

from datetime import UTC, datetime, timedelta

import pytest

from megavolt.entsoe import PricePoint
from megavolt.warehouse import WarehouseError, dsn, payload_hash, store_day_ahead_prices

ZONE = "10YAT-APG------L"
POINT = PricePoint(datetime(2026, 9, 11, 0, 0, tzinfo=UTC), 177.39)


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
