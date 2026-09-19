"""Hourly weather for one point in Austria, from the GeoSphere Austria data hub.

Source: GeoSphere Austria, dataset `inca-v1-1h-1km` (INCA analysis), licensed CC BY 4.0,
https://data.hub.geosphere.at. No key is needed; the hub allows 240 requests an hour, and a whole
year for one point is a single request, so ask for a window at once and never day by day.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime

import httpx

BASE_URL = "https://dataset.api.hub.geosphere.at/v1/timeseries/historical"
DATASET = "inca-v1-1h-1km"

# What the platform uses: air temperature in degree Celsius, global radiation in W m-2.
PARAMETERS = ("T2M", "GL")
# The same ranges as the table constraint and the dbt tests. Austria's records are about -53 and
# +41 degrees; the sun delivers about 1361 W m-2 above the atmosphere.
RANGES = {"T2M": (-60.0, 50.0), "GL": (0.0, 1400.0)}

REQUEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# A year of two parameters is about 0.3 MB.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GeosphereError(RuntimeError):
    """Raised when the data hub returns something we cannot use."""


@dataclass(frozen=True, slots=True)
class WeatherPoint:
    """One value of one parameter at one hour."""

    valid_at: datetime
    parameter: str
    value: float


def fetch(start: datetime, end: datetime, latitude: float, longitude: float) -> bytes:
    """Perform one GET for a point and a window, and return the raw body."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("window boundaries must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    query = {
        "parameters": ",".join(PARAMETERS),
        "lat_lon": f"{latitude},{longitude}",
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "output_format": "geojson",
    }
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            with client.stream("GET", f"{BASE_URL}/{DATASET}", params=query) as response:
                if response.status_code != httpx.codes.OK:
                    # Printable and on one line, so server text cannot forge a log line.
                    detail = _plain(next(response.iter_text(), ""))
                    raise GeosphereError(f"data hub returned HTTP {response.status_code}: {detail}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise GeosphereError(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
                return bytes(body)
    except httpx.HTTPError as exc:
        raise GeosphereError(f"request to the data hub failed: {exc}") from None


def _plain(text: str) -> str:
    """At most 300 printable characters on one line."""
    return "".join(ch if ch.isprintable() else " " for ch in text[:300])


def in_range(points: list[WeatherPoint]) -> tuple[list[WeatherPoint], list[WeatherPoint]]:
    """Split points into those inside their physical range and those outside it.

    The table refuses a value outside the range, and it refuses the whole batch with it. So the
    caller stores the good points first and complains about the odd ones afterwards: one
    strange hour must be loud, and must not keep ten good days out of the warehouse.
    """
    good, odd = [], []
    for point in points:
        low, high = RANGES[point.parameter]
        (good if low <= point.value <= high else odd).append(point)
    return good, odd


def parse(body: bytes | str) -> list[WeatherPoint]:
    """Turn a timeseries answer into points. A null is a missing hour, never a zero."""
    try:
        document = json.loads(body)
        timestamps = [datetime.fromisoformat(stamp) for stamp in document["timestamps"]]
        (feature,) = document["features"]
        series = feature["properties"]["parameters"]
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise GeosphereError(
            f"answer does not have the expected shape: {_plain(repr(exc))}"
        ) from None

    if any(stamp.tzinfo is None for stamp in timestamps):
        raise GeosphereError("answer carries a timestamp without a time zone")
    # Two values for one hour would be stored with the same clock and win by accident.
    if len(set(timestamps)) != len(timestamps):
        raise GeosphereError("answer carries the same timestamp twice")
    # A parameter left out would read as a window of missing hours, without any error.
    if not isinstance(series, dict) or set(series) != set(PARAMETERS):
        raise GeosphereError(f"answer does not carry exactly {PARAMETERS}")

    points: list[WeatherPoint] = []
    for parameter, content in series.items():
        values = content.get("data") if isinstance(content, dict) else None
        # A shorter or longer array would put every later value on the wrong hour.
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise GeosphereError(f"{parameter} does not line up with the timestamps")
        for stamp, value in zip(timestamps, values, strict=True):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise GeosphereError(f"{parameter} at {stamp:%Y-%m-%d %H:%M} is not a number")
            # The magnitude test also stops an integer too long for isfinite to take.
            if isinstance(value, float) and not math.isfinite(value) or abs(value) > 1e6:
                raise GeosphereError(f"{parameter} at {stamp:%Y-%m-%d %H:%M} is not finite")
            points.append(WeatherPoint(stamp, parameter, float(value)))

    if not points:
        raise GeosphereError("answer contained no values")
    return sorted(points, key=lambda point: (point.valid_at, point.parameter))


def weather(
    start: datetime, end: datetime, latitude: float, longitude: float
) -> list[WeatherPoint]:
    """Fetch hourly temperature and global radiation for a point over a window, end inclusive."""
    points = parse(fetch(start, end, latitude, longitude))
    if not all(start <= point.valid_at <= end for point in points):
        raise GeosphereError("answer carries hours outside the window that was asked for")
    return points
