"""Client for the ENTSO-E Transparency Platform RESTful API.

Endpoint, token handling and date format verified against the official knowledge base:
https://transparencyplatform.zendesk.com/hc/en-us/sections/12783116987028-Web-API
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree

BASE_URL = "https://web-api.tp.entsoe.eu/api"
TOKEN_ENV = "ENTSOE_API_TOKEN"  # noqa: S105 - the name of an environment variable, not a secret

AT_BIDDING_ZONE = "10YAT-APG------L"

DOC_TYPE_DAY_AHEAD_PRICES = "A44"

# ENTSO-E publishes more than one price sequence per delivery day. Sequence 1 is the day-ahead
# coupling result; what sequence 2 holds is not stated in the official documentation.
DAY_AHEAD_SEQUENCE = "1"

REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

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


def redact(text: str) -> str:
    """Replace any security token in the text, so it is safe to log or raise."""
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
                    response.read()
                    raise EntsoeError(
                        f"ENTSO-E returned HTTP {response.status_code}: "
                        f"{redact(response.text)[:500]}"
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
        raise EntsoeError(f"request to ENTSO-E failed: {redact(str(exc))}") from exc


def parse_price_document(xml: bytes | str) -> list[PricePoint]:
    """Turn a Publication_MarketDocument into price points, without inventing missing ones."""
    try:
        root = ElementTree.fromstring(xml)
    except Exception as exc:
        raise EntsoeError(f"response was not parseable XML: {exc}") from exc

    if _local(root.tag) == "Acknowledgement_MarketDocument":
        raise EntsoeError(f"ENTSO-E rejected the query: {_text(root, 'text') or 'no reason given'}")

    prices: dict[datetime, float] = {}
    for series in (node for node in root.iter() if _local(node.tag) == "TimeSeries"):
        curve_type = _text(series, "curveType")
        for period in (node for node in series.iter() if _local(node.tag) == "Period"):
            for point in _parse_period(period, curve_type):
                seen = prices.setdefault(point.interval_start, point.price_eur_mwh)
                if seen != point.price_eur_mwh:
                    raise EntsoeError(
                        f"two different prices for {point.interval_start:%Y-%m-%d %H:%M}: "
                        f"{seen} and {point.price_eur_mwh}"
                    )

    if not prices:
        raise EntsoeError("document contained no price points")
    return [PricePoint(start, price) for start, price in sorted(prices.items())]


def _parse_period(period, curve_type: str | None) -> list[PricePoint]:
    """Expand one Period into one point per interval, carrying values forward only for A03."""
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

    prices: dict[int, float] = {}
    for point in period.iter():
        if _local(point.tag) != "Point":
            continue
        position, amount = _text(point, "position"), _text(point, "price.amount")
        if position is None or amount is None:
            raise EntsoeError("point is missing its position or its price")
        prices[int(position)] = float(amount)

    result: list[PricePoint] = []
    carried: float | None = None
    for position in range(1, expected + 1):
        if position in prices:
            carried = prices[position]
        elif curve_type != "A03" or carried is None:
            continue
        result.append(PricePoint(start + (position - 1) * step, carried))
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
