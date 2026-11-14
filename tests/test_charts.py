"""The server-drawn charts: shape of the data in, shape of the SVG out."""

from datetime import UTC, datetime, timedelta

import pytest

from megavolt_tool import charts


def test_the_day_profile_averages_each_local_hour():
    start = datetime(2025, 6, 1, 22, tzinfo=UTC)  # midnight Vienna on 2 June
    settled = [(start + timedelta(minutes=15 * i), 0.0, float(i % 96 // 4)) for i in range(2 * 96)]
    profile = charts.day_profile(settled)
    assert len(profile) == 24 and profile[0] == 0.0 and profile[23] == 23.0


def test_columns_draw_24_bars_with_a_title_each_and_one_label():
    svg = str(charts.columns([0.1] * 23 + [0.4], "kWh"))
    assert svg.count("<rect") == 24 and svg.count("<title>") == 24
    assert "0.40 kWh" in svg and "00:00" in svg and svg.count('rx="4"') == 24


def test_bars_scale_to_the_largest_and_label_every_amount():
    svg = str(charts.bars([("a", 100.0, "#000", None), ("b", 300.0, "#111", 500.0)], "€"))
    assert "300 € expected · up to 500 €" in svg and "100 €" in svg and svg.count("<rect") == 3


def test_names_are_escaped_before_they_reach_the_svg():
    svg = str(charts.bars([("<b>x</b>", 1.0, "#000", None)], "€"))
    assert "<b>" not in svg and "&lt;b&gt;" in svg


def test_monthly_bills_price_each_quarter_hour_at_the_market_or_at_the_fixed_price():
    jan = datetime(2025, 1, 10, 12, tzinfo=UTC)
    feb = datetime(2025, 2, 1, 12, tzinfo=UTC)
    settled = [(jan, 0.0, 2.0), (feb, 0.0, 1.0)]
    prices = [(jan, 100.0), (feb, 200.0)]  # EUR/MWh
    settled = [(jan + timedelta(days=d), 0.0, 2.0) for d in range(20)] + [
        (feb + timedelta(days=d), 0.0, 1.0) for d in range(20)
    ]
    prices = [(when, 100.0 if when.month == 1 else 200.0) for when, _, _ in settled]
    market = charts.monthly_bills(settled, prices, fee_eur_kwh=0.01)
    assert [m for m, _ in market][-3:] == ["Dec", "Jan\n25", "Feb"] and len(market) == 12
    assert market[0][0] == "Mar\n24"  # the year under the first month and under January
    assert market[-2][1] == pytest.approx(20 * 2.0 * 0.11)
    assert market[-1][1] == pytest.approx(20 * 1.0 * 0.21)
    assert market[0][1] == 0.0  # a month without data stays on the axis, empty
    fixed = charts.monthly_bills(settled, prices, fixed_eur_kwh=0.2)
    assert fixed[-1][1] == pytest.approx(20 * 0.2)
    assert charts.monthly_bills([(jan, 0.0, 1.0)], []) == []  # no price, no bill
    # Fewer than 20 settled days: the month shows as empty, never as a small bar.
    assert charts.monthly_bills(settled[:5], prices, fixed_eur_kwh=0.2)[-1][1] == 0.0


def test_columns_take_their_own_labels():
    svg = str(charts.columns([10.0, 300.0], "€", labels=["01/25", "02/25"]))
    assert "02/25" in svg and "300 €" in svg


def test_a_month_without_data_is_an_empty_outline_not_a_bar():
    svg = str(charts.columns([0.0, 300.0], "€", labels=["01/25", "02/25"]))
    assert "no data" in svg and 'fill="none"' in svg and svg.count("<rect") == 2


def test_gridline_steps_are_round():
    assert [charts._step(p) for p in (0.4, 86.17, 2802.0)] == [0.1, 20, 1000]


def test_ranges_place_the_band_and_the_median_on_one_scale_and_say_it_in_words():
    svg = charts.ranges([("household", 10.0, 20.0, 40.0), ("<b>", 20.0, 20.0, 20.0)], "c/kWh")
    assert 'x="25.00%"' in svg and 'width="75.00%"' in svg and '<svg x="50.00%"' in svg
    assert "household 10.00 c/kWh to 40.00 c/kWh, median 20.00 c/kWh" in svg
    assert "<b>" not in svg and "&lt;b&gt;" in svg
