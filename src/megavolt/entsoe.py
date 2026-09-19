"""Client for the ENTSO-E Transparency Platform RESTful API.

Endpoint, token handling and date format verified against the official knowledge base:
https://transparencyplatform.zendesk.com/hc/en-us/sections/12783116987028-Web-API
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree

BASE_URL = "https://web-api.tp.entsoe.eu/api"
TOKEN_ENV = "ENTSOE_API_TOKEN"  # noqa: S105 - the name of an environment variable, not a secret

AT_BIDDING_ZONE = "10YAT-APG------L"

DOC_TYPE_DAY_AHEAD_PRICES = "A44"
DOC_TYPE_ACTUAL_LOAD = "A65"
DOC_TYPE_IMBALANCE_PRICES = "A85"
PROCESS_TYPE_REALISED = "A16"

# Probed against the live API on 2026-09-19: imbalance prices arrive as a ZIP of one
# Balancing_MarketDocument per request, with one TimeSeries per direction. The code meanings come
# from the ENTSO-E code list and the entsoe-py parsers.
IMBALANCE_CATEGORIES = {"A04": "long", "A05": "short"}
DOC_STATUSES = {"A01": "intermediate", "A02": "final"}

# ENTSO-E publishes more than one price sequence per delivery day. Sequence 1 is the day-ahead
# coupling result; what sequence 2 holds is not stated in the official documentation.
DAY_AHEAD_SEQUENCE = "1"

REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ZIP_MEMBERS = 8
# A year of quarter hours. A period claiming more is not a period we asked for.
MAX_PERIOD_POINTS = 35_136

# At INFO httpx logs every request URL, and ours carries the token as a query parameter.
logging.getLogger("httpx").setLevel(logging.WARNING)

_RESOLUTIONS = {
    "PT15M": timedelta(minutes=15),
    "PT30M": timedelta(minutes=30),
    "PT60M": timedelta(hours=1),
    "P1D": timedelta(days=1),
}

_TOKEN_PATTERN = re.compile(r"(securityToken=)[^&\s\"']+", re.IGNORECASE)


class EntsoeError(RuntimeError):
    """Raised when ENTSO-E returns something we cannot use."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A single day-ahead price and the interval it applies to."""

    interval_start: datetime
    price_eur_mwh: float


@dataclass(frozen=True, slots=True)
class LoadPoint:
    """The actual total load of a bidding zone in one interval, in MW."""

    interval_start: datetime
    load_mw: float


@dataclass(frozen=True, slots=True)
class ImbalancePricePoint:
    """One imbalance price for one direction, with the status of the document it came in."""

    interval_start: datetime
    category: str
    price_eur_mwh: float
    doc_status: str


def redact(text: str) -> str:
    """Replace any security token in the text, so it is safe to log or raise."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        text = text.replace(token, "***")
    return _TOKEN_PATTERN.sub(r"\1***", text)


def format_period(moment: datetime) -> str:
    """Format a datetime as the yyyyMMddHHmm string in UTC that the API expects."""
    if moment.tzinfo is None:
        raise ValueError("period boundaries must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def _token() -> str:
    """Read the API token from the environment, or explain how to set it."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise EntsoeError(
            f"{TOKEN_ENV} is not set. Copy .env.example to .env and put your token there."
        )
    return token


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag, whose version changes between documents."""
    return tag.rpartition("}")[2]


def _first(element, name: str):
    """Return the first descendant with this local tag name, or None."""
    return next((node for node in element.iter() if _local(node.tag) == name), None)


def _text(element, name: str) -> str | None:
    """Return the text of the first descendant with this local tag name, or None."""
    node = _first(element, name)
    return None if node is None or node.text is None else node.text.strip()


def fetch(params: dict[str, str]) -> bytes:
    """Perform one authenticated GET against the API and return the raw response body."""
    query = {**params, "securityToken": _token()}
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            with client.stream("GET", BASE_URL, params=query) as response:
                if response.status_code != httpx.codes.OK:
                    # Only the first chunk: an error body is for a human, and untrusted.
                    detail = next(response.iter_text(), "")[:500]
                    raise EntsoeError(
                        f"ENTSO-E returned HTTP {response.status_code}: {redact(detail)}"
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise EntsoeError(
                            f"response exceeded {MAX_RESPONSE_BYTES} bytes, refusing to buffer it"
                        )
                return bytes(body)
    except httpx.HTTPError as exc:
        # `from None`: the httpx exception holds the request, and its URL holds the token.
        raise EntsoeError(f"request to ENTSO-E failed: {redact(str(exc))}") from None


def _root(xml: bytes | str):
    """Parse one market document, or explain why it is not one."""
    try:
        root = ElementTree.fromstring(xml)
    except Exception as exc:
        raise EntsoeError(f"response was not parseable XML: {exc}") from exc

    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = redact(_text(root, "text") or "no reason given")
        raise EntsoeError(f"ENTSO-E rejected the query: {reason}")
    return root


def _documents(body: bytes) -> list[bytes]:
    """Unpack a ZIP response into its documents; a plain document is returned as is."""
    if not body.startswith(b"PK"):
        return [body]
    documents = []
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise EntsoeError(f"ZIP response has {len(members)} members, refusing to unpack it")
        remaining = MAX_RESPONSE_BYTES
        for member in members:
            # Declared sizes are never trusted: the cap is on the bytes actually read, in total.
            with archive.open(member) as stream:
                content = stream.read(remaining + 1)
            remaining -= len(content)
            if remaining < 0:
                raise EntsoeError(f"ZIP response unpacks to more than {MAX_RESPONSE_BYTES} bytes")
            documents.append(content)
    if not documents:
        raise EntsoeError("ZIP response contained no documents")
    return documents


def _values(root, value_tag: str, key=lambda series, start: start) -> dict[object, float]:
    """Collect one value per key across all series, refusing two different values for a key."""
    values: dict[object, float] = {}
    for series in (node for node in root.iter() if _local(node.tag) == "TimeSeries"):
        curve_type = _text(series, "curveType")
        for period in (node for node in series.iter() if _local(node.tag) == "Period"):
            for start, value in _parse_period(period, curve_type, value_tag):
                seen = values.setdefault(key(series, start), value)
                if seen != value:
                    raise EntsoeError(
                        f"two different values for {start:%Y-%m-%d %H:%M}: {seen} and {value}"
                    )
                if len(values) > MAX_PERIOD_POINTS:
                    raise EntsoeError("document expands to more points than a year holds")
    if not values:
        raise EntsoeError("document contained no points")
    return values


def parse_price_document(xml: bytes | str) -> list[PricePoint]:
    """Turn a Publication_MarketDocument into price points, without inventing missing ones."""
    prices = _values(_root(xml), "price.amount")
    return [PricePoint(start, price) for start, price in sorted(prices.items())]


def parse_load_document(xml: bytes | str) -> list[LoadPoint]:
    """Turn a GL_MarketDocument into load points, in MW."""
    loads = _values(_root(xml), "quantity")
    return [LoadPoint(start, load) for start, load in sorted(loads.items())]


def _category(series, start) -> tuple[str, datetime]:
    """Key a value by the one price direction its series carries, refusing anything else."""
    categories = {
        (node.text or "").strip()
        for node in series.iter()
        if _local(node.tag) == "imbalance_Price.category"
    }
    if len(categories) != 1 or not categories <= IMBALANCE_CATEGORIES.keys():
        raise EntsoeError(f"time series has unusable price categories: {sorted(categories)}")
    return next(iter(categories)), start


def parse_imbalance_document(body: bytes) -> list[ImbalancePricePoint]:
    """Turn a Balancing_MarketDocument, or a ZIP of them, into points per interval and direction."""
    points: dict[tuple[str, datetime], ImbalancePricePoint] = {}
    for document in _documents(body):
        root = _root(document)
        status_node = _first(root, "docStatus")
        status = None if status_node is None else _text(status_node, "value")
        if status not in DOC_STATUSES:
            raise EntsoeError(f"imbalance document has an unknown status: {status!r}")
        for (category, start), price in _values(root, "imbalance_Price.amount", _category).items():
            point = ImbalancePricePoint(start, category, price, status)
            # Two documents in one answer may repeat an interval, never disagree about it.
            if points.setdefault((category, start), point) != point:
                raise EntsoeError(f"two documents disagree about {start:%Y-%m-%d %H:%M}")
    return sorted(points.values(), key=lambda point: (point.interval_start, point.category))


def _parse_period(period, curve_type: str | None, value_tag: str) -> list[tuple[datetime, float]]:
    """Expand one Period into one value per interval, carrying values forward only for A03."""
    interval = _first(period, "timeInterval")
    resolution = _text(period, "resolution")
    if interval is None or resolution not in _RESOLUTIONS:
        raise EntsoeError(f"period has an unusable resolution: {resolution!r}")

    step = _RESOLUTIONS[resolution]
    bounds = _text(interval, "start"), _text(interval, "end")
    if None in bounds:
        raise EntsoeError("period is missing its time interval bounds")
    start, end = (datetime.fromisoformat(bound) for bound in bounds)
    expected = int((end - start) / step)
    if not 0 < expected <= MAX_PERIOD_POINTS:
        raise EntsoeError(f"period claims {expected} intervals, refusing to expand it")

    values: dict[int, float] = {}
    for point in period.iter():
        if _local(point.tag) != "Point":
            continue
        position, value = _text(point, "position"), _text(point, value_tag)
        if position is None or value is None:
            raise EntsoeError(f"point is missing its position or its {value_tag}")
        number = float(value)
        if not math.isfinite(number):
            raise EntsoeError(f"point {position} has a {value_tag} that is not a number: {value!r}")
        values[int(position)] = number
    if values and not 1 <= min(values) <= max(values) <= expected:
        raise EntsoeError(f"period of {expected} intervals has a position outside it")

    result: list[tuple[datetime, float]] = []
    carried: float | None = None
    for position in range(1, expected + 1):
        if position in values:
            carried = values[position]
        elif curve_type != "A03" or carried is None:
            continue
        result.append((start + (position - 1) * step, carried))
    return result


def day_ahead_prices(
    start: datetime, end: datetime, domain: str = AT_BIDDING_ZONE
) -> list[PricePoint]:
    """Fetch day-ahead prices for a bidding zone over a half-open UTC interval."""
    if end <= start:
        raise ValueError("end must be after start")
    return parse_price_document(
        fetch(
            {
                "documentType": DOC_TYPE_DAY_AHEAD_PRICES,
                "in_Domain": domain,
                "out_Domain": domain,
                "periodStart": format_period(start),
                "periodEnd": format_period(end),
                "ClassificationSequence_AttributeInstanceComponent.Position": DAY_AHEAD_SEQUENCE,
            }
        )
    )


def actual_load(start: datetime, end: datetime, domain: str = AT_BIDDING_ZONE) -> list[LoadPoint]:
    """Fetch the realised total load of a bidding zone over a half-open UTC interval."""
    if end <= start:
        raise ValueError("end must be after start")
    return parse_load_document(
        fetch(
            {
                "documentType": DOC_TYPE_ACTUAL_LOAD,
                "processType": PROCESS_TYPE_REALISED,
                "outBiddingZone_Domain": domain,
                "periodStart": format_period(start),
                "periodEnd": format_period(end),
            }
        )
    )


def imbalance_prices(
    start: datetime, end: datetime, control_area: str = AT_BIDDING_ZONE
) -> list[ImbalancePricePoint]:
    """Fetch imbalance prices for a control area over a half-open UTC interval.

    One document carries one status, so ask for one day at a time if the status matters per day.
    """
    if end <= start:
        raise ValueError("end must be after start")
    return parse_imbalance_document(
        fetch(
            {
                "documentType": DOC_TYPE_IMBALANCE_PRICES,
                "controlArea_Domain": control_area,
                "periodStart": format_period(start),
                "periodEnd": format_period(end),
            }
        )
    )


def main() -> None:
    """Print today's Austrian day-ahead prices, as a smoke test of the whole path."""
    from dotenv import load_dotenv

    load_dotenv()
    vienna = ZoneInfo("Europe/Vienna")
    midnight = datetime.now(vienna).replace(hour=0, minute=0, second=0, microsecond=0)
    prices = day_ahead_prices(midnight, midnight + timedelta(days=1))
    print(f"{len(prices)} price points for AT, {midnight:%Y-%m-%d} Vienna time\n")
    for point in prices:
        local = point.interval_start.astimezone(vienna)
        print(f"  {local:%H:%M}  {point.price_eur_mwh:>8.2f} EUR/MWh")


if __name__ == "__main__":
    main()
