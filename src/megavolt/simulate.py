"""The truth model: what the community actually did in each quarter hour.

Every number here is drawn from `sha256(seed | what | when)`, never from a global generator and
never from `hash()`, which Python salts per process. A replay of the same day therefore produces
the same bytes, which is the only reason `ON CONFLICT DO NOTHING` can absorb a repeat instead of
duplicating it.

The noise is not decoration. If every household were an exact multiple of one profile and the
plant followed E1 exactly, then `allocated_i` would be exactly proportional to `C_i` in every
interval and forecasting our residual would be arithmetic rather than a problem. Per-day level
factors, per-interval jitter and per-day cloud cover are what make the residual worth modelling.

Every multiplicative factor is drawn with mean exactly 1, so the annual totals stay on the figures
signed off in D22 in expectation. They are not renormalised to hit them exactly: that would need
the whole year simulated before any single day could be produced.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, date, datetime, time, timedelta

from megavolt.community import GENERATION_SEGMENT, VIENNA, Member
from megavolt.readings import RESOLUTION

# Spread of the daily level of one member: a cold week, a holiday, somebody working from home.
DAY_LEVEL_SIGMA = 0.12
# Spread within a day: a kettle, a car charging, a machine starting.
INTERVAL_SIGMA = 0.08
# Cloud cover over the plant. Wider, and clipped, because a day is either sunny or it is not.
CLOUD_SIGMA = 0.35
CLOUD_FLOOR = 0.10
CLOUD_CEILING = 1.30

# Wh precision on a kWh figure, which is what a real meter delivers.
DECIMALS = 4

PROFILE_NORMALISATION = 1000.0


class SimulationError(RuntimeError):
    """Raised when the simulator is asked for something it cannot produce honestly."""


def rng(*parts: object) -> random.Random:
    """A generator seeded only by what it is generating, so the draw is reproducible."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))  # noqa: S311 - seeded on purpose


def _unit_lognormal(generator: random.Random, sigma: float) -> float:
    """A positive factor whose mean is exactly 1, so it scales without shifting the total."""
    return generator.lognormvariate(-0.5 * sigma * sigma, sigma)  # noqa: S311 - seeded on purpose


def day_intervals(day: date) -> tuple[datetime, ...]:
    """The UTC interval starts covering one Austrian local day: 92, 96 or 100 of them.

    Asking Vienna rather than counting 96 is what makes the daylight saving days correct without
    a special case anywhere else in the platform.
    """
    start = datetime.combine(day, time.min, VIENNA).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, VIENNA).astimezone(UTC)
    count = int((end - start) / RESOLUTION)
    return tuple(start + position * RESOLUTION for position in range(count))


def _profile_values(
    profiles: dict[str, dict[datetime, float]], profile_type: str, intervals: tuple[datetime, ...]
) -> tuple[float, ...]:
    """Look up one profile over one day, refusing to invent a value that is not stored."""
    series = profiles.get(profile_type)
    if series is None:
        raise SimulationError(f"no {profile_type} profile was loaded")
    try:
        return tuple(series[interval] for interval in intervals)
    except KeyError as exc:
        raise SimulationError(f"the {profile_type} profile has no value for {exc.args[0]}") from exc


def consumption_series(
    member: Member,
    intervals: tuple[datetime, ...],
    profiles: dict[str, dict[datetime, float]],
    seed: str,
) -> tuple[float, ...]:
    """What one member consumed in each quarter hour of one day."""
    shape = _profile_values(profiles, member.profile_type, intervals)
    scale = member.annual_kwh / PROFILE_NORMALISATION

    day = intervals[0].astimezone(VIENNA).date()
    level = _unit_lognormal(rng(seed, "day_level", member.metering_point, day), DAY_LEVEL_SIGMA)

    jitter = rng(seed, "interval", member.metering_point, day)
    return tuple(
        round(value * scale * level * _unit_lognormal(jitter, INTERVAL_SIGMA), DECIMALS)
        for value in shape
    )


def generation_series(
    plant: Member,
    intervals: tuple[datetime, ...],
    profiles: dict[str, dict[datetime, float]],
    seed: str,
) -> tuple[float, ...]:
    """What the community plant generated in each quarter hour of one day."""
    if plant.segment != GENERATION_SEGMENT:
        raise SimulationError(f"{plant.metering_point} is not the generation point")

    shape = _profile_values(profiles, plant.profile_type, intervals)
    scale = plant.annual_kwh / PROFILE_NORMALISATION

    day = intervals[0].astimezone(VIENNA).date()
    clouds = _unit_lognormal(rng(seed, "clouds", day), CLOUD_SIGMA)
    clouds = min(max(clouds, CLOUD_FLOOR), CLOUD_CEILING)

    return tuple(round(value * scale * clouds, DECIMALS) for value in shape)


def allocate(
    consumption: dict[str, tuple[float, ...]], generation: tuple[float, ...]
) -> dict[str, tuple[float, ...]]:
    """Share the generation between members: `allocated_i = C_i * min(1, G / C)`.

    This is the community's step, before ours. Whatever it does not cover is our volume to buy
    and bill, which is why a sunny afternoon collapses our position without anybody consuming
    less. The cap is what makes it a share rather than a gift.
    """
    totals = [
        sum(series[position] for series in consumption.values())
        for position in range(len(generation))
    ]
    shares = [
        0.0 if total <= 0 else min(1.0, made / total)
        for made, total in zip(generation, totals, strict=True)
    ]

    allocated = {}
    for point, series in consumption.items():
        # min() again after rounding: a value rounded up must never exceed what was consumed,
        # because the database refuses that row and would abort the whole batch.
        allocated[point] = tuple(
            min(round(used * share, DECIMALS), used)
            for used, share in zip(series, shares, strict=True)
        )
    return allocated


def surplus(
    generation: tuple[float, ...], consumption: dict[str, tuple[float, ...]]
) -> tuple[float, ...]:
    """What nobody used and went back to the grid: `max(0, G - C)`."""
    return tuple(
        round(max(0.0, made - sum(series[position] for series in consumption.values())), DECIMALS)
        for position, made in enumerate(generation)
    )


def community_totals(consumption: dict[str, tuple[float, ...]]) -> tuple[float, ...]:
    """The community's own consumption total per interval, which is all we are told about it."""
    length = len(next(iter(consumption.values())))
    return tuple(
        round(sum(series[position] for series in consumption.values()), DECIMALS)
        for position in range(length)
    )


def simulate_day(
    day: date,
    members: tuple[Member, ...],
    profiles: dict[str, dict[datetime, float]],
    seed: str,
) -> tuple[
    tuple[datetime, ...],
    dict[str, tuple[float, ...]],
    dict[str, tuple[float, ...]],
    tuple[float, ...],
]:
    """One day of truth: the intervals, everyone's consumption, their allocation, and generation."""
    intervals = day_intervals(day)
    if not intervals:
        raise SimulationError(f"{day} has no intervals")

    consumers = [member for member in members if member.segment != GENERATION_SEGMENT]
    plants = [member for member in members if member.segment == GENERATION_SEGMENT]
    if len(plants) != 1:
        raise SimulationError(f"expected exactly one generation point, found {len(plants)}")

    consumption = {
        member.metering_point: consumption_series(member, intervals, profiles, seed)
        for member in consumers
    }
    generation = generation_series(plants[0], intervals, profiles, seed)
    return intervals, consumption, allocate(consumption, generation), generation


def _demo() -> None:
    """Print one day of the truth model, so the shape can be eyeballed without a database."""
    import sys

    from dotenv import load_dotenv

    load_dotenv()

    from megavolt.community import members as registry
    from megavolt.community import seed as default_seed
    from megavolt.warehouse import load_profiles

    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2025, 3, 1)
    people = registry()
    wanted = sorted({member.profile_type for member in people})
    profiles = load_profiles(day.year, wanted)

    intervals, consumption, allocated, generation = simulate_day(
        day, people, profiles, default_seed()
    )
    totals = community_totals(consumption)
    spare = surplus(generation, consumption)

    print(f"{day} in Vienna: {len(intervals)} intervals, {len(consumption)} members\n")
    print("  interval (UTC)      generation   consumption   allocated     surplus")
    for position in range(0, len(intervals), 8):
        share = sum(series[position] for series in allocated.values())
        print(
            f"  {intervals[position]:%Y-%m-%d %H:%M}  {generation[position]:>10.2f}"
            f"  {totals[position]:>12.2f}  {share:>10.2f}  {spare[position]:>10.2f}"
        )

    covered = sum(sum(series) for series in allocated.values())
    used = sum(totals)
    made = sum(generation)
    print(f"\n  day totals: generated {made:.1f} kWh, consumed {used:.1f} kWh")
    print(f"  community covered {covered:.1f} kWh = {covered / used:.1%} of its consumption")
    print(f"  our volume is what is left of our customers' share of that {used:.1f} kWh")


if __name__ == "__main__":
    _demo()
