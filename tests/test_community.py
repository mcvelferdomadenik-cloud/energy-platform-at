"""Tests for the community registry. Pure functions, no database and no network."""

import hashlib
from collections import Counter

import pytest

from megavolt.community import (
    GENERATION_PROFILE,
    GENERATION_SEGMENT,
    GENERATION_SHARE,
    METERING_POINT_LENGTH,
    OUR_CUSTOMERS,
    PLANT_POSTAL_CODE,
    POSTAL_CODES,
    SEGMENTS,
    members,
)

REGISTRY = members("test-seed")
CONSUMERS = [member for member in REGISTRY if member.segment != GENERATION_SEGMENT]


def test_the_registry_is_six_hundred_members_plus_one_generation_point():
    assert len(REGISTRY) == 601
    assert len(CONSUMERS) == 600


def test_the_segment_mix_is_exactly_the_one_that_was_decided():
    assert Counter(member.segment for member in CONSUMERS) == {
        "household": 360,
        "business": 210,
        "municipality": 30,
    }


def test_the_profile_mix_is_exactly_the_one_that_was_decided():
    assert Counter(member.profile_type for member in CONSUMERS) == {
        "H0": 360,
        "G0": 120,
        "G1": 78,
        "G3": 30,
        "B1": 12,
    }


def test_exactly_three_hundred_and_fifty_members_are_ours():
    assert sum(member.ours for member in CONSUMERS) == OUR_CUSTOMERS


def test_the_generation_point_is_never_ours_and_is_the_only_photovoltaic_one():
    photovoltaic = [m for m in REGISTRY if m.profile_type == GENERATION_PROFILE]
    assert len(photovoltaic) == 1
    assert photovoltaic[0].segment == GENERATION_SEGMENT
    assert photovoltaic[0].ours is False


def test_the_same_seed_produces_an_identical_registry():
    assert members("test-seed") == REGISTRY


def test_a_different_seed_produces_a_different_registry():
    assert members("another-seed") != REGISTRY


def test_every_metering_point_is_a_thirty_three_character_austrian_identifier():
    for member in REGISTRY:
        assert len(member.metering_point) == METERING_POINT_LENGTH
        assert member.metering_point.startswith("AT")
        assert member.metering_point.isalnum()


def test_members_live_in_every_postal_code_of_the_community_and_nowhere_else():
    codes = Counter(member.metering_point[8:13] for member in CONSUMERS)
    assert set(codes) == {f"0{code}" for code in POSTAL_CODES}
    assert max(codes, key=codes.get) == "0" + max(POSTAL_CODES, key=POSTAL_CODES.get)


def test_the_postal_code_shares_add_up_to_the_whole_community():
    assert sum(POSTAL_CODES.values()) == pytest.approx(1.0)


def test_the_plant_stands_in_its_own_postal_code():
    assert REGISTRY[-1].metering_point[8:13] == f"0{PLANT_POSTAL_CODE}"


def test_where_members_live_does_not_change_what_they_consume():
    # The sizes that the forecast's community figures were taken from, for the default seed.
    total = sum(member.annual_kwh for member in members("megavolt")[:-1])
    assert total == pytest.approx(4_560_200, abs=50)


def test_where_members_live_does_not_change_who_is_ours_or_how_large_anybody_is():
    # The fingerprint of the registry as it was before members had postal codes of their own.
    text = "|".join(
        f"{m.annual_kwh}:{int(m.ours)}:{m.meter_id}:{m.profile_type}" for m in members("megavolt")
    )
    assert hashlib.sha256(text.encode()).hexdigest() == (
        "a006228b606031d6bb8f19eea8292d83ed8cbcda3b307af0214494d01d2b2a77"
    )


def test_no_two_members_share_a_metering_point_or_a_meter():
    assert len({member.metering_point for member in REGISTRY}) == len(REGISTRY)
    assert len({member.meter_id for member in REGISTRY}) == len(REGISTRY)


def test_no_annual_consumption_falls_outside_its_segment_bounds():
    bounds = {(spec.segment, spec.profile_type): spec for spec in SEGMENTS}
    for member in CONSUMERS:
        spec = bounds[member.segment, member.profile_type]
        assert spec.lowest_kwh <= member.annual_kwh <= spec.highest_kwh


def test_annual_generation_is_the_decided_share_of_annual_consumption():
    consumption = sum(member.annual_kwh for member in CONSUMERS)
    generation = REGISTRY[-1].annual_kwh
    assert generation == pytest.approx(GENERATION_SHARE * consumption, abs=0.1)
