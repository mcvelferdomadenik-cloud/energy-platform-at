"""Tests for the ENTSO-E client that run without a token and without network access."""

import io
import zipfile
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from megavolt.entsoe import (
    EntsoeError,
    ImbalancePricePoint,
    LoadPoint,
    PricePoint,
    _token,
    format_period,
    parse_imbalance_document,
    parse_load_document,
    parse_price_document,
    redact,
)

NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0"


def time_series(curve_type: str, positions: dict[int, float]) -> str:
    """Build one three-hour TimeSeries containing only these positions."""
    points = "".join(
        f"<Point><position>{position}</position><price.amount>{amount}</price.amount></Point>"
        for position, amount in positions.items()
    )
    return f"""
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
  </TimeSeries>"""


def document_with(*series: str) -> str:
    """Wrap time series into a Publication_MarketDocument, as ENTSO-E sends several at once."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="{NS}">{"".join(series)}
</Publication_MarketDocument>"""


def price_document(curve_type: str, positions: dict[int, float]) -> str:
    """Build a document holding a single time series."""
    return document_with(time_series(curve_type, positions))


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


def test_parse_collapses_a_time_series_that_entsoe_sent_twice():
    series = time_series("A01", {1: 85.5, 2: 90.0, 3: 78.25})
    points = parse_price_document(document_with(series, series))
    assert [point.price_eur_mwh for point in points] == [85.5, 90.0, 78.25]


def test_parse_refuses_two_different_prices_for_the_same_interval():
    document = document_with(
        time_series("A01", {1: 85.5, 2: 90.0, 3: 78.25}),
        time_series("A01", {1: 190.0, 2: 90.0, 3: 78.25}),
    )
    with pytest.raises(EntsoeError, match="two different values"):
        parse_price_document(document)


def test_parse_raises_on_an_empty_document():
    with pytest.raises(EntsoeError, match="no points"):
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


# --- actual load and imbalance prices ------------------------------------------------------------

GL_NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"
BAL_NS = "urn:iec62325.351:tc57wg16:451-6:balancingdocument:4:4"


def load_document(positions: dict[int, float]) -> str:
    """A GL_MarketDocument for one hour of 15-minute load, as ENTSO-E sends it (curve type A03)."""
    points = "".join(
        f"<Point><position>{p}</position><quantity>{q}</quantity></Point>"
        for p, q in positions.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument xmlns="{GL_NS}">
  <TimeSeries>
    <quantity_Measure_Unit.name>MAW</quantity_Measure_Unit.name>
    <curveType>A03</curveType>
    <Period>
      <timeInterval><start>2025-03-30T23:00Z</start><end>2025-03-31T00:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      {points}
    </Period>
  </TimeSeries>
</GL_MarketDocument>"""


def imbalance_series(category: str, prices: list[float]) -> str:
    """One direction of imbalance prices for half an hour, two quarter hours."""
    points = "".join(
        f"<Point><position>{i}</position><imbalance_Price.amount>{price}</imbalance_Price.amount>"
        f"<imbalance_Price.category>{category}</imbalance_Price.category></Point>"
        for i, price in enumerate(prices, start=1)
    )
    return f"""
  <TimeSeries>
    <businessType>A19</businessType>
    <curveType>A03</curveType>
    <Period>
      <timeInterval><start>2025-03-30T23:00Z</start><end>2025-03-30T23:30Z</end></timeInterval>
      <resolution>PT15M</resolution>
      {points}
    </Period>
  </TimeSeries>"""


def imbalance_document(status: str, *series: str) -> bytes:
    """A Balancing_MarketDocument with the given status."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Balancing_MarketDocument xmlns="{BAL_NS}">
  <docStatus><value>{status}</value></docStatus>{"".join(series)}
</Balancing_MarketDocument>""".encode()


def zipped(*documents: bytes) -> bytes:
    """Documents packed the way the imbalance endpoint delivers them."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for number, document in enumerate(documents, start=1):
            archive.writestr(f"{number:03d}-IMBALANCE_PRICES.xml", document)
    return buffer.getvalue()


def test_load_carries_a_missing_quarter_hour_forward_as_entsoe_intends():
    points = parse_load_document(load_document({1: 5429.2, 2: 5400.0, 4: 5389.0}))
    assert [point.load_mw for point in points] == [5429.2, 5400.0, 5400.0, 5389.0]
    assert points[0] == LoadPoint(datetime(2025, 3, 30, 23, 0, tzinfo=UTC), 5429.2)


def test_load_refuses_an_acknowledgement():
    with pytest.raises(EntsoeError, match="No matching data found"):
        parse_load_document(ACKNOWLEDGEMENT)


def test_imbalance_prices_come_out_of_the_zip_with_direction_and_status():
    long, short = imbalance_series("A04", [103.75, 70.76]), imbalance_series("A05", [103.75, 70.76])
    points = parse_imbalance_document(zipped(imbalance_document("A02", long, short)))
    assert points == [
        ImbalancePricePoint(datetime(2025, 3, 30, 23, 0, tzinfo=UTC), "A04", 103.75, "A02"),
        ImbalancePricePoint(datetime(2025, 3, 30, 23, 0, tzinfo=UTC), "A05", 103.75, "A02"),
        ImbalancePricePoint(datetime(2025, 3, 30, 23, 15, tzinfo=UTC), "A04", 70.76, "A02"),
        ImbalancePricePoint(datetime(2025, 3, 30, 23, 15, tzinfo=UTC), "A05", 70.76, "A02"),
    ]


def test_imbalance_prices_keep_different_prices_per_direction_apart():
    body = imbalance_document(
        "A01", imbalance_series("A04", [50.0]), imbalance_series("A05", [250.0])
    )
    points = parse_imbalance_document(body)
    assert {point.category: point.price_eur_mwh for point in points} == {"A04": 50.0, "A05": 250.0}
    assert {point.doc_status for point in points} == {"A01"}


def test_imbalance_prices_refuse_a_status_we_do_not_know():
    with pytest.raises(EntsoeError, match="unknown status"):
        parse_imbalance_document(imbalance_document("A09", imbalance_series("A04", [1.0])))


def test_imbalance_prices_refuse_an_unknown_direction():
    with pytest.raises(EntsoeError, match="price categories"):
        parse_imbalance_document(imbalance_document("A02", imbalance_series("A06", [1.0])))


def test_imbalance_prices_refuse_two_different_prices_for_the_same_direction():
    body = imbalance_document(
        "A02", imbalance_series("A04", [50.0]), imbalance_series("A04", [51.0])
    )
    with pytest.raises(EntsoeError, match="two different values"):
        parse_imbalance_document(body)


def test_imbalance_zip_with_an_acknowledgement_inside_is_refused():
    with pytest.raises(EntsoeError, match="No matching data found"):
        parse_imbalance_document(zipped(ACKNOWLEDGEMENT.encode()))


def test_a_value_that_is_not_a_number_is_refused_before_it_can_be_stored():
    for junk in ("NaN", "inf", "1e999"):
        with pytest.raises(EntsoeError, match="not a number"):
            parse_load_document(load_document({1: junk}))


def test_a_period_claiming_centuries_is_refused_instead_of_expanded():
    document = load_document({1: 5400.0}).replace("2025-03-31T00:00Z", "2425-03-31T00:00Z")
    with pytest.raises(EntsoeError, match="refusing to expand"):
        parse_load_document(document)


def test_a_zip_with_too_many_members_is_refused():
    document = imbalance_document("A02", imbalance_series("A04", [1.0]))
    with pytest.raises(EntsoeError, match="members"):
        parse_imbalance_document(zipped(*[document] * 9))


def test_a_zip_that_unpacks_beyond_the_cap_is_refused(monkeypatch):
    document = imbalance_document("A02", imbalance_series("A04", [1.0]))
    monkeypatch.setattr("megavolt.entsoe.MAX_RESPONSE_BYTES", len(document) * 2 - 1)
    with pytest.raises(EntsoeError, match="unpacks to more than"):
        parse_imbalance_document(zipped(document, document))


def test_a_rejection_that_echoes_the_request_never_shows_the_token():
    echoed = ACKNOWLEDGEMENT.replace(
        "No matching data found", "No data for securityToken=abc123&x=1"
    )
    with pytest.raises(EntsoeError) as raised:
        parse_load_document(echoed)
    assert "abc123" not in str(raised.value)


def test_two_documents_in_one_answer_may_repeat_an_interval_but_not_disagree():
    same = imbalance_document("A02", imbalance_series("A04", [50.0]))
    other = imbalance_document("A02", imbalance_series("A04", [51.0]))
    assert len(parse_imbalance_document(zipped(same, same))) == 2
    with pytest.raises(EntsoeError, match="disagree"):
        parse_imbalance_document(zipped(same, other))


def test_a_position_outside_its_period_is_refused_rather_than_dropped():
    with pytest.raises(EntsoeError, match="outside"):
        parse_load_document(load_document({1: 5400.0, 9: 5500.0}))


def test_redact_hides_the_bare_token_even_without_its_parameter_name(monkeypatch):
    monkeypatch.setenv("ENTSOE_API_TOKEN", "abc123")
    assert "abc123" not in redact("the server echoed abc123 back")
