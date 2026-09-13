"""Tests for the APCS profile reader that run without network access."""

import io
import zipfile
from datetime import UTC, datetime

import pytest

from megavolt.apcs import ApcsError, ProfilePoint, parse_profiles, profile_names

CATEGORIES = "Typnummer;Typname;Typtext\n1;H0;Haushalt\n15;E1;Photovoltaik\n"

DATA = (
    "Typnummer;Zeit;Wert\n"
    "1;2024-12-31T23:15:00;0,02721\n"
    "1;2024-12-31T23:30:00;0,02522\n"
    "15;2025-06-21T10:45:00;0,12700\n"
)


def archive(categories: str = CATEGORIES, data: str = DATA, year: int = 2025) -> bytes:
    """Build an in-memory archive shaped like the one APCS publishes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Kategorien.csv", categories.encode("cp1252"))
        zf.writestr(f"synthload{year}.csv", data.encode("cp1252"))
    return buffer.getvalue()


def test_labels_are_read_as_the_end_of_the_interval():
    first = next(parse_profiles(archive(), 2025))
    assert first.interval_start == datetime(2024, 12, 31, 23, 0, tzinfo=UTC)


def test_the_first_interval_of_the_year_is_austrian_local_midnight():
    first = next(parse_profiles(archive(), 2025))
    assert first.interval_start.astimezone().utcoffset() is not None
    assert first.interval_start.isoformat() == "2024-12-31T23:00:00+00:00"


def test_the_german_decimal_comma_becomes_a_float():
    assert next(parse_profiles(archive(), 2025)).value == pytest.approx(0.02721)


def test_the_type_number_is_resolved_to_the_profile_name():
    points = list(parse_profiles(archive(), 2025))
    assert [point.profile_type for point in points] == ["H0", "H0", "E1"]


def test_only_the_requested_types_come_back():
    points = list(parse_profiles(archive(), 2025, types=frozenset({"E1"})))
    assert points == [ProfilePoint("E1", datetime(2025, 6, 21, 10, 30, tzinfo=UTC), 0.127)]


def test_profile_names_maps_each_type_to_its_description():
    assert profile_names(archive()) == {"H0": "Haushalt", "E1": "Photovoltaik"}


def test_a_row_with_the_wrong_field_count_is_refused_not_skipped():
    with pytest.raises(ApcsError, match="expected three fields"):
        list(parse_profiles(archive(data="Typnummer;Zeit;Wert\n1;2025-01-01T00:15:00\n"), 2025))


def test_an_unknown_profile_number_is_refused():
    unknown = "Typnummer;Zeit;Wert\n99;2025-01-01T00:15:00;0,1\n"
    with pytest.raises(ApcsError, match="unknown profile type"):
        list(parse_profiles(archive(data=unknown), 2025))


def test_an_unreadable_value_is_refused():
    with pytest.raises(ApcsError, match="unreadable row"):
        list(parse_profiles(archive(data="Typnummer;Zeit;Wert\n1;2025-01-01T00:15:00;x\n"), 2025))


def test_an_unexpected_header_is_refused():
    with pytest.raises(ApcsError, match="unexpected header"):
        list(parse_profiles(archive(data="a;b;c\n1;2025-01-01T00:15:00;0,1\n"), 2025))


def test_a_missing_data_member_is_refused():
    with pytest.raises(ApcsError, match="exactly one member"):
        list(parse_profiles(archive(year=2024), 2025))
