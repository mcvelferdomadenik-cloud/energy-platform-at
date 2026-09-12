"""The community MegaVolt sells into: 600 members, 350 of them our customers.

Everything here is derived from one seed, so the same seed always produces the same registry.
The simulator knows all 600 members because the community does; the pipeline only ever sees the
350 that are ours plus the community aggregates (D20).

Metering point identifiers follow the Austrian Zählpunktbezeichnung: 33 characters, `AT` plus a
six-digit grid operator number, plus the four-digit postal code padded to five, plus twenty
alphanumeric characters the grid operator assigns freely.
Source: https://www.e-control.at/konsumenten/rechnung/aufbau-inhalte

The grid operator number used here is deliberately outside the range Austrian operators are
assigned, so a generated identifier can never collide with a real metering point.

The annual consumption figures are modelling assumptions, not measurements. Only the household
median has a commonly quoted Austrian reference value; the business and municipality medians are
chosen to be plausible and are labelled as assumptions wherever they surface.
"""

from __future__ import annotations

import base64
import hashlib
import math
import os
import random
from dataclasses import dataclass, replace
from datetime import datetime
from zoneinfo import ZoneInfo

SEED_ENV = "SIMULATOR_SEED"
DEFAULT_SEED = "megavolt"

GRID_OPERATOR = "099999"
POSTAL_CODE = "08430"
METERING_POINT_LENGTH = 33

VIENNA = ZoneInfo("Europe/Vienna")
VALID_FROM = datetime(2025, 1, 1, tzinfo=VIENNA)

GENERATION_SEGMENT = "generation"
GENERATION_PROFILE = "E1"
GENERATION_SHARE = 0.45
YIELD_KWH_PER_KWP = 1000.0

OUR_CUSTOMERS = 350


@dataclass(frozen=True, slots=True)
class Segment:
    """How many members of one kind there are, and how large they are."""

    segment: str
    profile_type: str
    count: int
    median_kwh: float
    sigma: float
    lowest_kwh: float
    highest_kwh: float


@dataclass(frozen=True, slots=True)
class Member:
    """One metering point in the community. The generation plant is one of these too."""

    metering_point: str
    profile_type: str
    segment: str
    annual_kwh: float
    meter_id: str
    ours: bool


# 60% households, 35% business, 5% municipality of 600 members, sized as a village community:
# small trades rather than industry, so households carry a share of the volume that actually
# matters. Only the household median has a commonly quoted Austrian reference value.
SEGMENTS = (
    Segment("household", "H0", 360, 3500, 0.45, 900, 15000),
    Segment("business", "G0", 120, 6000, 0.60, 1500, 30000),
    Segment("business", "G1", 60, 12000, 0.60, 2500, 60000),
    Segment("business", "G3", 30, 25000, 0.60, 5000, 125000),
    Segment("municipality", "B1", 12, 18000, 0.35, 5000, 60000),
    Segment("municipality", "G1", 18, 25000, 0.50, 6000, 100000),
)


def seed() -> str:
    """The seed every draw derives from, so a replay produces the same registry."""
    return os.environ.get(SEED_ENV, "").strip() or DEFAULT_SEED


def metering_point(index: int, community_seed: str) -> str:
    """Build one Austrian metering point identifier, deterministic in the index."""
    digest = hashlib.sha256(f"{community_seed}|metering_point|{index}".encode()).digest()
    tail = base64.b32encode(digest).decode()[:20]
    return f"AT{GRID_OPERATOR}{POSTAL_CODE}{tail}"


def _annual_kwh(rng: random.Random, spec: Segment) -> float:
    """Draw one annual consumption from a lognormal, clipped to a plausible range."""
    # noqa below: a seeded Random is the point here - the registry must be reproducible.
    drawn = rng.lognormvariate(math.log(spec.median_kwh), spec.sigma)  # noqa: S311
    return round(min(max(drawn, spec.lowest_kwh), spec.highest_kwh), 1)


def members(community_seed: str | None = None) -> tuple[Member, ...]:
    """Build the 600 consuming members plus the community's single generation point."""
    community_seed = community_seed or seed()
    rng = random.Random(f"{community_seed}|registry")  # noqa: S311 - see _annual_kwh

    consumers: list[Member] = []
    for spec in SEGMENTS:
        for _ in range(spec.count):
            index = len(consumers)
            consumers.append(
                Member(
                    metering_point=metering_point(index, community_seed),
                    profile_type=spec.profile_type,
                    segment=spec.segment,
                    annual_kwh=_annual_kwh(rng, spec),
                    meter_id=f"MV-{index:06d}-A",
                    ours=False,
                )
            )

    ours = set(rng.sample(range(len(consumers)), OUR_CUSTOMERS))
    consumers = [replace(member, ours=index in ours) for index, member in enumerate(consumers)]

    return (*consumers, _generation_point(consumers, community_seed))


def _generation_point(consumers: list[Member], community_seed: str) -> Member:
    """Size the community plant so annual generation is GENERATION_SHARE of consumption."""
    index = len(consumers)
    annual = round(GENERATION_SHARE * sum(member.annual_kwh for member in consumers), 1)
    return Member(
        metering_point=metering_point(index, community_seed),
        profile_type=GENERATION_PROFILE,
        segment=GENERATION_SEGMENT,
        annual_kwh=annual,
        meter_id=f"MV-{index:06d}-A",
        ours=False,
    )


def main() -> None:
    """Print the registry as a sanity check, without touching the database."""
    registry = members()
    consumers = [member for member in registry if member.segment != GENERATION_SEGMENT]
    plant = registry[-1]

    def line(label: str, group: list[Member]) -> None:
        total = sum(member.annual_kwh for member in group) / 1000
        print(f"  {label:<14} {len(group):>4} points  {total:>9.1f} MWh/year")

    print(f"seed {seed()!r}: {len(registry)} metering points, {len(consumers)} consuming\n")
    for name in ("household", "business", "municipality"):
        line(name, [member for member in consumers if member.segment == name])
    line("all members", consumers)
    line("of those ours", [member for member in consumers if member.ours])

    kwp = plant.annual_kwh / YIELD_KWH_PER_KWP
    print(f"\n  generation     {plant.annual_kwh / 1000:>19.1f} MWh/year ({GENERATION_SHARE:.0%})")
    print(f"  implied plant  {kwp:>19.0f} kWp at ~{YIELD_KWH_PER_KWP:.0f} kWh/kWp")
    first = registry[0].metering_point
    print(f"\n  first metering point {first} ({len(first)} chars)")


if __name__ == "__main__":
    main()
