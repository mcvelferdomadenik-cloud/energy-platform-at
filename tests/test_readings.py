"""Tests for the message contract. No broker, no database, no network.

Every case here is something a message could arrive looking like. A message is the one place
untrusted bytes enter the platform, so each rule gets its own test rather than a shared one.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from megavolt.readings import (
    COMMUNITY_SCHEMA,
    MAX_MESSAGE_BYTES,
    MAX_POINT_KWH,
    READING_SCHEMA,
    MessageError,
    community_batch,
    decode,
    encode,
    reading_batch,
    readings_of,
    validate_community_batch,
    validate_reading_batch,
)

POINT = "AT09999908430BUZOSQIZOKT57LP7GYW2"
METER = "MV-000042-A"
DELIVERED = datetime(2025, 3, 3, 3, 30, tzinfo=UTC)
START = datetime(2025, 3, 1, 23, 0, tzinfo=UTC)


def batch(consumption=None, allocated=None, **overrides):
    """A valid 96-interval reading batch, with fields replaced for the case under test."""
    consumption = [0.1] * 96 if consumption is None else consumption
    allocated = [0.0] * 96 if allocated is None else allocated
    payload = reading_batch(POINT, METER, 1, DELIVERED, START, consumption, allocated)
    payload.update(overrides)
    return payload


# --- encoding: the property the whole replay guarantee rests on -----------------------------


def test_the_same_content_always_encodes_to_the_same_bytes():
    assert encode(batch()) == encode(batch())


def test_the_order_fields_were_written_in_does_not_change_the_bytes():
    one = batch()
    other = {key: one[key] for key in reversed(list(one))}
    assert encode(other) == encode(one)


def test_a_batch_survives_a_round_trip():
    assert decode(encode(batch())) == batch()


def test_a_not_a_number_is_refused_on_the_way_out():
    with pytest.raises(ValueError, match="Out of range|not JSON compliant"):
        encode(batch(consumption=[float("nan")] + [0.1] * 95))


def test_a_naive_timestamp_is_refused_on_the_way_out():
    with pytest.raises(MessageError, match="naive timestamp"):
        reading_batch(POINT, METER, 1, datetime(2025, 3, 3, 3, 30), START, [0.1] * 96, [0.0] * 96)


# --- decoding: what arrives is bytes somebody else wrote ------------------------------------


def test_an_oversized_message_is_refused_before_it_is_parsed():
    with pytest.raises(MessageError, match="exceeds"):
        decode(b"x" * (MAX_MESSAGE_BYTES + 1))


def test_not_a_number_in_an_incoming_message_is_refused():
    with pytest.raises(MessageError, match="NaN is not an acceptable value"):
        decode(b'{"schema":"x","value":NaN}')


def test_infinity_in_an_incoming_message_is_refused():
    with pytest.raises(MessageError, match="Infinity is not an acceptable value"):
        decode(b'{"schema":"x","value":Infinity}')


def test_a_json_array_is_not_a_message():
    with pytest.raises(MessageError, match="expected a JSON object"):
        decode(b"[1, 2, 3]")


def test_bytes_that_are_not_json_are_refused():
    with pytest.raises(MessageError, match="not parseable JSON"):
        decode(b"<html>503 Service Unavailable</html>")


# --- the readings a valid message delivers --------------------------------------------------


def test_every_interval_follows_the_previous_one_by_a_quarter_hour():
    readings = validate_reading_batch(batch())
    assert len(readings) == 96
    assert readings[0].interval_start == START
    assert readings[1].interval_start == START + timedelta(minutes=15)
    assert readings[-1].interval_start == START + timedelta(minutes=15 * 95)


def test_an_absent_interval_produces_no_reading_rather_than_a_zero():
    consumption = [0.1] * 96
    allocated = [0.0] * 96
    consumption[5] = allocated[5] = None
    readings = validate_reading_batch(batch(consumption, allocated))
    assert len(readings) == 95
    assert all(reading.interval_start != START + timedelta(minutes=75) for reading in readings)


def test_a_sparse_correction_delivers_only_the_intervals_it_touches():
    consumption = [None] * 96
    allocated = [None] * 96
    consumption[10], allocated[10] = 0.9, 0.4
    readings = validate_reading_batch(batch(consumption, allocated, version=2))
    assert len(readings) == 1
    assert readings[0].version == 2
    assert readings[0].consumption_kwh == 0.9


def test_a_message_that_delivers_nothing_at_all_is_refused():
    with pytest.raises(MessageError, match="no intervals at all"):
        validate_reading_batch(batch([None] * 96, [None] * 96))


def test_both_daylight_saving_day_lengths_are_accepted():
    for length in (92, 100):
        readings = validate_reading_batch(batch([0.1] * length, [0.0] * length))
        assert len(readings) == length


# --- what a message is not allowed to say ---------------------------------------------------


def test_a_message_without_the_expected_schema_tag_is_refused():
    with pytest.raises(MessageError, match="expected schema"):
        validate_reading_batch(batch(schema="meter_reading_batch.v2"))


def test_a_message_at_another_resolution_is_refused():
    with pytest.raises(MessageError, match="15 minute intervals"):
        validate_reading_batch(batch(resolution_minutes=60))


@pytest.mark.parametrize(
    "bad",
    [
        "AT09999908430BUZOSQIZOKT57LP7GY",  # 32 characters
        "AT09999908430buzosqizokt57lp7gyw2",  # lowercase
        "DE09999908430BUZOSQIZOKT57LP7GYW2",  # not Austrian
        "",
        None,
        42,
    ],
)
def test_a_metering_point_of_the_wrong_shape_is_refused(bad):
    with pytest.raises(MessageError, match="metering_point"):
        validate_reading_batch(batch(metering_point=bad))


@pytest.mark.parametrize("bad", ["MV-42-A", "MV-000042-", "meter", None])
def test_a_meter_identifier_of_the_wrong_shape_is_refused(bad):
    with pytest.raises(MessageError, match="meter_id"):
        validate_reading_batch(batch(meter_id=bad))


@pytest.mark.parametrize("bad", [0, -1, 1.5, "1", True, None])
def test_a_version_that_is_not_a_positive_whole_number_is_refused(bad):
    with pytest.raises(MessageError, match="version"):
        validate_reading_batch(batch(version=bad))


def test_a_timestamp_without_a_timezone_is_refused():
    with pytest.raises(MessageError, match="no timezone"):
        validate_reading_batch(batch(delivered_at="2025-03-03T03:30:00"))


def test_a_timestamp_from_before_the_window_we_accept_is_refused():
    with pytest.raises(MessageError, match="outside the window"):
        validate_reading_batch(batch(delivered_at="1999-03-03T03:30:00Z"))


def test_a_timestamp_far_in_the_future_is_refused():
    far = (datetime.now(UTC) + timedelta(days=400)).isoformat()
    with pytest.raises(MessageError, match="outside the window"):
        validate_reading_batch(batch(delivered_at=far))


def test_an_interval_start_off_the_quarter_hour_is_refused():
    with pytest.raises(MessageError, match="not on a quarter hour"):
        validate_reading_batch(batch(interval_start="2025-03-01T23:07:00Z"))


def test_an_array_of_the_wrong_length_is_refused():
    with pytest.raises(MessageError, match="has 95 intervals"):
        validate_reading_batch(batch([0.1] * 95, [0.0] * 95))


def test_two_arrays_of_different_lengths_are_refused():
    with pytest.raises(MessageError, match="consumption has 96 intervals and allocated has 92"):
        validate_reading_batch(batch([0.1] * 96, [0.0] * 92))


def test_something_that_is_not_an_array_is_refused():
    # Set the field directly: the builder would coerce a mapping to a list of its keys.
    payload = batch()
    payload["consumption"] = {"0": 0.1}
    with pytest.raises(MessageError, match="not an array"):
        validate_reading_batch(payload)


@pytest.mark.parametrize("bad", [-0.5, MAX_POINT_KWH + 1, "0.1", True, [0.1]])
def test_an_amount_that_is_not_a_plausible_number_of_kilowatt_hours_is_refused(bad):
    consumption = [0.1] * 96
    consumption[3] = bad
    with pytest.raises(MessageError, match=r"consumption\[3\]"):
        validate_reading_batch(batch(consumption))


def test_an_interval_that_delivers_only_one_of_the_two_values_is_refused():
    consumption = [0.1] * 96
    allocated = [0.0] * 96
    allocated[7] = None
    with pytest.raises(MessageError, match="only one of consumption and allocated"):
        validate_reading_batch(batch(consumption, allocated))


def test_allocating_more_than_was_consumed_is_refused_before_the_database_sees_it():
    consumption = [0.1] * 96
    allocated = [0.0] * 96
    allocated[9] = 0.5
    with pytest.raises(MessageError, match="which is impossible"):
        validate_reading_batch(batch(consumption, allocated))


# --- the community's own message ------------------------------------------------------------


def test_the_community_message_carries_totals_and_no_metering_point():
    payload = community_batch(1, DELIVERED, START, [0.0] * 96, [130.0] * 96)
    assert "metering_point" not in payload
    intervals = validate_community_batch(payload)
    assert len(intervals) == 96
    assert intervals[0].generation_kwh == 0.0
    assert intervals[0].consumption_kwh == 130.0


def test_the_community_may_generate_more_than_it_consumes():
    payload = community_batch(1, DELIVERED, START, [500.0] * 96, [130.0] * 96)
    assert validate_community_batch(payload)[0].generation_kwh == 500.0


def test_a_message_is_routed_by_what_it_says_it_is():
    assert readings_of(batch())[0].metering_point == POINT
    community = community_batch(1, DELIVERED, START, [0.0] * 96, [1.0] * 96)
    assert readings_of(community)[0].interval_start == START


def test_an_unknown_schema_is_refused_rather_than_guessed_at():
    with pytest.raises(MessageError, match="unknown schema"):
        readings_of({"schema": "something_else.v1"})


def test_the_two_schema_names_are_not_accidentally_the_same():
    assert READING_SCHEMA != COMMUNITY_SCHEMA


def test_an_unknown_field_is_ignored_rather_than_refused():
    payload = batch()
    payload["sent_by"] = "a future version of the producer"
    assert len(validate_reading_batch(payload)) == 96


def test_the_encoder_and_the_validator_agree_on_what_a_batch_is():
    assert len(validate_reading_batch(json.loads(encode(batch())))) == 96
