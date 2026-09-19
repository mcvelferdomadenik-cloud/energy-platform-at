"""Tests for the GeoSphere Austria client that run without network access."""

import json
from datetime import UTC, datetime

import pytest

from megavolt.geosphere import GeosphereError, WeatherPoint, fetch, parse

HOURS = ["2025-03-30T10:00+00:00", "2025-03-30T11:00+00:00", "2025-03-30T12:00+00:00"]


def answer(temperature, radiation, timestamps=HOURS) -> str:
    """A timeseries answer shaped like the data hub sends it."""
    return json.dumps(
        {
            "timestamps": timestamps,
            "features": [
                {
                    "geometry": {"type": "Point", "coordinates": [15.5463, 46.7761]},
                    "properties": {
                        "parameters": {
                            "T2M": {"unit": "degree_Celsius", "data": temperature},
                            "GL": {"unit": "W m-2", "data": radiation},
                        }
                    },
                }
            ],
        }
    )


def test_every_value_comes_out_with_its_hour_and_its_parameter():
    points = parse(answer([12.1, 13.4, 14.0], [420.5, 610.0, 639.7]))
    assert len(points) == 6
    assert points[0] == WeatherPoint(datetime(2025, 3, 30, 10, tzinfo=UTC), "GL", 420.5)
    assert points[1] == WeatherPoint(datetime(2025, 3, 30, 10, tzinfo=UTC), "T2M", 12.1)


def test_a_missing_hour_stays_missing_and_never_becomes_a_zero():
    points = parse(answer([12.1, None, 14.0], [420.5, 610.0, None]))
    assert len(points) == 4
    assert all(point.value != 0 for point in points)
    assert (datetime(2025, 3, 30, 11, tzinfo=UTC), "T2M") not in {
        (point.valid_at, point.parameter) for point in points
    }


def test_a_night_hour_with_no_sun_is_a_real_zero_and_is_kept():
    points = parse(answer([3.0, 3.1, 3.2], [0.0, 0.0, 0.0]))
    assert [point.value for point in points if point.parameter == "GL"] == [0.0, 0.0, 0.0]


def test_an_array_that_does_not_line_up_with_the_hours_is_refused():
    with pytest.raises(GeosphereError, match="line up"):
        parse(answer([12.1, 13.4], [420.5, 610.0, 639.7]))


def test_a_value_that_is_not_a_finite_number_is_refused():
    for junk in ("12.1", True, float("inf")):
        with pytest.raises(GeosphereError, match="not a number|not finite"):
            parse(answer([junk, 13.4, 14.0], [1.0, 2.0, 3.0]))
    with pytest.raises(GeosphereError, match="not finite"):
        parse(answer([12.1, 13.4, 14.0], [1.0, 2.0, 3.0]).replace("13.4", "NaN"))


def test_a_timestamp_without_a_time_zone_is_refused():
    with pytest.raises(GeosphereError, match="time zone"):
        parse(answer([1.0], [2.0], timestamps=["2025-03-30T10:00"]))


def test_a_parameter_nobody_asked_for_is_refused():
    document = json.loads(answer([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]))
    document["features"][0]["properties"]["parameters"]["RR"] = {"data": [0.0, 0.0, 0.0]}
    with pytest.raises(GeosphereError, match="exactly"):
        parse(json.dumps(document))


def test_an_error_page_or_an_empty_answer_is_refused():
    for junk in (b"<html>Service Unavailable</html>", b"{}", b'{"timestamps": [], "features": []}'):
        with pytest.raises(GeosphereError):
            parse(junk)
    with pytest.raises(GeosphereError, match="no values"):
        parse(answer([None, None, None], [None, None, None]))


def test_a_window_without_a_time_zone_or_running_backwards_is_refused_before_any_request():
    aware = datetime(2025, 3, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        fetch(datetime(2025, 3, 30), aware, 46.78, 15.54)
    with pytest.raises(ValueError, match="after start"):
        fetch(aware, aware, 46.78, 15.54)


def test_an_answer_that_leaves_a_parameter_out_is_refused_not_read_as_missing_hours():
    document = json.loads(answer([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]))
    del document["features"][0]["properties"]["parameters"]["GL"]
    with pytest.raises(GeosphereError, match="exactly"):
        parse(json.dumps(document))


def test_the_same_hour_twice_in_one_answer_is_refused():
    with pytest.raises(GeosphereError, match="twice"):
        parse(answer([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], timestamps=[HOURS[0], HOURS[0], HOURS[1]]))


def test_hostile_shapes_fail_as_our_own_error_and_store_nothing():
    huge = answer([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]).replace(
        "2.0, 3.0]", "2.0, " + "9" * 400 + "]", 1
    )
    nested = "[" * 100_000 + "]" * 100_000
    as_list = json.loads(answer([1.0], [2.0], timestamps=HOURS[:1]))
    as_list["features"][0]["properties"]["parameters"] = [1, 2]
    for junk in (huge, nested, json.dumps(as_list)):
        with pytest.raises(GeosphereError):
            parse(junk)


def test_an_answer_with_hours_outside_the_window_is_refused(monkeypatch):
    from megavolt import geosphere

    monkeypatch.setattr(geosphere, "fetch", lambda *_: answer([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]))
    inside = geosphere.weather(
        datetime(2025, 3, 30, 10, tzinfo=UTC), datetime(2025, 3, 30, 12, tzinfo=UTC), 46.78, 15.54
    )
    assert len(inside) == 6
    with pytest.raises(GeosphereError, match="outside the window"):
        geosphere.weather(
            datetime(2025, 3, 30, 11, tzinfo=UTC),
            datetime(2025, 3, 30, 12, tzinfo=UTC),
            46.78,
            15.54,
        )


def test_a_value_outside_its_range_is_set_aside_without_losing_the_good_ones():
    from megavolt.geosphere import RANGES, in_range

    points = parse(answer([12.1, 55.0, 14.0], [420.5, -0.3, 1400.0]))
    good, odd = in_range(points)
    assert {(point.parameter, point.value) for point in odd} == {("T2M", 55.0), ("GL", -0.3)}
    assert len(good) == 4
    assert all(RANGES[p.parameter][0] <= p.value <= RANGES[p.parameter][1] for p in good)


def test_the_ranges_are_the_same_in_the_client_the_table_and_the_dbt_tests():
    import re
    from pathlib import Path

    from megavolt.geosphere import RANGES

    root = Path(__file__).resolve().parents[1]
    schema = (root / "db" / "init" / "01_schema.sql").read_text(encoding="utf-8")
    in_table = {
        name: (float(low), float(high))
        for name, low, high in re.findall(
            r"parameter = '(\w+)' AND value BETWEEN (-?\d+) AND (-?\d+)", schema
        )
    }
    assert in_table == RANGES

    model = (root / "dbt" / "models" / "staging" / "staging.yml").read_text(encoding="utf-8")
    block = model[
        model.index("- name: stg_weather_hourly") : model.index("- name: stg_load_profile")
    ]
    bounds = [float(x) for x in re.findall(r"(?:min|max)_value: (-?\d+)", block)]
    assert bounds == [*RANGES["T2M"], *RANGES["GL"]]


def test_server_text_in_an_error_is_short_printable_and_on_one_line():
    from megavolt.geosphere import _plain

    folded = _plain("line one\nFAKE LOG LINE\x1b[31m red" + "x" * 5000)
    assert len(folded) <= 300 and folded.isprintable()
