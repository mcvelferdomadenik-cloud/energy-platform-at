"""Client for the official APCS synthetic load profiles.

These are the profiles Austria uses for real imbalance settlement of small customers.
Labels in the file are UTC and mark the END of each interval, established from the file
itself: every day carries 96 values including the daylight saving changes, and the first
and last labels bracket the Austrian calendar year only under end-labelling.

Source: https://www.apcs.at/de/clearing/technisches-clearing/lastprofile
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

ARCHIVE_URL = (
    "https://www.apcs.at/fileadmin/user_upload/APCS/Clearing/Lastprofile/synthload{year}.zip"
)

REQUEST_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 128 * 1024 * 1024

RESOLUTION = timedelta(minutes=15)
ENCODING = "cp1252"

HOUSEHOLD = "H0"
PHOTOVOLTAIC = "E1"


class ApcsError(RuntimeError):
    """Raised when the APCS archive cannot be fetched or read."""


@dataclass(frozen=True, slots=True)
class ProfilePoint:
    """One quarter hour of one profile, normalised so a year sums to 1000."""

    profile_type: str
    interval_start: datetime
    value: float


def download(year: int) -> bytes:
    """Fetch one year of profiles, refusing an archive larger than the cap."""
    url = ARCHIVE_URL.format(year=year)
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                if response.status_code != httpx.codes.OK:
                    response.read()
                    raise ApcsError(f"APCS returned HTTP {response.status_code} for {url}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_ARCHIVE_BYTES:
                        raise ApcsError(f"archive exceeded {MAX_ARCHIVE_BYTES} bytes")
                return bytes(body)
    except httpx.HTTPError as exc:
        raise ApcsError(f"request to APCS failed: {exc}") from exc


def _member(archive: zipfile.ZipFile, suffix: str) -> str:
    """Find the archive member whose name ends with this suffix, case-insensitively."""
    matches = [n for n in archive.namelist() if n.lower().endswith(suffix)]
    if len(matches) != 1:
        raise ApcsError(f"expected exactly one member ending in {suffix!r}, found {len(matches)}")
    return matches[0]


def _read_member(archive: zipfile.ZipFile, name: str) -> Iterator[str]:
    """Stream one member as text, refusing to decompress more than the cap."""
    read = 0
    with archive.open(name) as raw:
        for line in io.TextIOWrapper(raw, encoding=ENCODING, newline=""):
            read += len(line)
            if read > MAX_MEMBER_BYTES:
                raise ApcsError(f"member {name!r} exceeded {MAX_MEMBER_BYTES} bytes")
            yield line


def profile_names(archive_bytes: bytes) -> dict[str, str]:
    """Map profile type to its German description, from the categories member."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        rows = csv.reader(_read_member(archive, _member(archive, "kategorien.csv")), delimiter=";")
        next(rows, None)
        return {row[1].strip(): row[2].strip() for row in rows if len(row) >= 3}


def parse_profiles(
    archive_bytes: bytes, year: int, types: frozenset[str] | None = None
) -> Iterator[ProfilePoint]:
    """Yield every quarter-hour value, or only those of the requested profile types."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        numbers = _type_numbers(archive)
        member = _member(archive, f"synthload{year}.csv")
        rows = csv.reader(_read_member(archive, member), delimiter=";")
        header = next(rows, None)
        if header is None or [c.strip() for c in header] != ["Typnummer", "Zeit", "Wert"]:
            raise ApcsError(f"unexpected header in the {year} profile member: {header}")
        for row in rows:
            if not row:
                continue
            point = _parse_row(row, numbers)
            if types is None or point.profile_type in types:
                yield point


def _type_numbers(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map the numeric type id used in the data to the profile name."""
    rows = csv.reader(_read_member(archive, _member(archive, "kategorien.csv")), delimiter=";")
    next(rows, None)
    return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}


def _parse_row(row: list[str], numbers: dict[str, str]) -> ProfilePoint:
    """Turn one data row into a point, failing loudly rather than skipping it."""
    if len(row) != 3:
        raise ApcsError(f"expected three fields, got {len(row)}: {row}")
    number, label, value = (field.strip() for field in row)
    if number not in numbers:
        raise ApcsError(f"unknown profile type number {number!r}")
    try:
        interval_end = datetime.fromisoformat(label).replace(tzinfo=UTC)
        amount = float(value.replace(",", "."))
    except ValueError as exc:
        raise ApcsError(f"unreadable row {row}: {exc}") from exc
    return ProfilePoint(numbers[number], interval_end - RESOLUTION, amount)


def main() -> None:
    """Download one year and print the annual sum of each profile, as a sanity check."""
    import sys

    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2025
    archive = download(year)
    names = profile_names(archive)

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for point in parse_profiles(archive, year):
        totals[point.profile_type] = totals.get(point.profile_type, 0.0) + point.value
        counts[point.profile_type] = counts.get(point.profile_type, 0) + 1

    print(f"{len(archive) / 1024 / 1024:.1f} MB, {sum(counts.values())} values, year {year}\n")
    for profile in sorted(totals):
        print(
            f"  {profile:<6} {totals[profile]:>10.4f}  {counts[profile]:>6} intervals"
            f"  {names.get(profile, '')[:40]}"
        )


if __name__ == "__main__":
    main()
