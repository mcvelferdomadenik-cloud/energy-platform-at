"""Tests for the ENTSO-E client that run without a token and without network access."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from megavolt.entsoe import (
    EntsoeError,
    PricePoint,
    _token,
    format_period,
    parse_price_document,
    redact,
)

NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0"


def price_document(curve_type: str, positions: dict[int, float]) -> str:
    """Build a three-hour Publication_MarketDocument containing only these positions."""
    points = "".join(
        f"<Point><position>{position}</position><price.amount>{amount}</price.amount></Point>"
        for position, amount in positions.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="{NS}">
  <TimeSeries>
    <curveType>{curve_type}</curveType>
    <Period>
      <timeInterval>
        <start>2026-09-10T00:00Z</start>
        <end>2026-09-10T03:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      {points}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""


ACKNOWLEDGEMENT = f"""<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="{NS}">
  <Reason>
    <code>999</code>
    <text>No matching data found</text>
  </Reason>
</Acknowledgement_MarketDocument>"""


def test_redact_hides_the_token_in_a_url():
    url = "https://web-api.tp.entsoe.eu/api?securityToken=abc123secret&documentType=A44"
    assert redact(url) == "https://web-api.tp.entsoe.eu/api?securityToken=***&documentType=A44"


def test_redact_hides_the_token_at_the_end_of_a_string():
    assert "supersecret" not in redact("failed for securityToken=supersecret")


def test_format_period_converts_to_utc():
    vienna = datetime(2026, 9, 10, 14, 0, tzinfo=ZoneInfo("Europe/Vienna"))
    assert format_period(vienna) == "202609101200"


def test_format_period_rejects_a_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        format_period(datetime(2026, 9, 10, 14, 0))


def test_parse_returns_one_point_per_interval():
    points = parse_price_document(price_document("A01", {1: 85.5, 2: 90.0, 3: 78.25}))
    assert points == [
        PricePoint(datetime(2026, 9, 10, 0, 0, tzinfo=UTC), 85.5),
        PricePoint(datetime(2026, 9, 10, 1, 0, tzinfo=UTC), 90.0),
        PricePoint(datetime(2026, 9, 10, 2, 0, tzinfo=UTC), 78.25),
    ]


def test_parse_carries_the_previous_price_forward_for_curve_type_a03():
    points = parse_price_document(price_document("A03", {1: 85.5, 3: 78.25}))
    assert [point.price_eur_mwh for point in points] == [85.5, 85.5, 78.25]


def test_parse_leaves_a_gap_visible_when_the_curve_type_does_not_repeat():
    points = parse_price_document(price_document("A01", {1: 85.5, 3: 78.25}))
    assert [point.interval_start.hour for point in points] == [0, 2]


def test_parse_raises_when_entsoe_acknowledges_instead_of_answering():
    with pytest.raises(EntsoeError, match="No matching data found"):
        parse_price_document(ACKNOWLEDGEMENT)


def test_parse_raises_on_an_empty_document():
    with pytest.raises(EntsoeError, match="no price points"):
        parse_price_document(price_document("A01", {}))


def test_parse_raises_on_junk():
    with pytest.raises(EntsoeError, match="not parseable XML"):
        parse_price_document(b"503 Service Unavailable")


def test_parse_refuses_an_error_page_instead_of_reading_it_as_zero_prices():
    with pytest.raises(EntsoeError):
        parse_price_document(b"<html><body>Service Unavailable</body></html>")


def test_missing_token_explains_how_to_fix_it(monkeypatch):
    monkeypatch.delenv("ENTSOE_API_TOKEN", raising=False)
    with pytest.raises(EntsoeError, match=".env.example"):
        _token()
