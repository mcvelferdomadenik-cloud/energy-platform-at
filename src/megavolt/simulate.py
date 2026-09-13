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
chosen for the community in expectation. They are not renormalised to hit them exactly: that
would need the whole year simulated before any single day could be produced.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta

from megavolt.community import GENERATION_SEGMENT, VALID_FROM, VIENNA, Member
from megavolt.readings import (
    READINGS_TOPIC,
    RESOLUTION,
    bootstrap_servers,
    community_batch,
    encode,
    reading_batch,
    utc_text,
)

# Kafka needs a key for every message and the community has no metering point to use.
COMMUNITY_KEY = "community"

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

# --- How badly the data arrives. Signed off 2026-09-12, modelling assumptions, not measurements.
# The grid operator sends previous-day values overnight, but not all of them and not all correct.

# A point-day that misses its slot and turns up one to three days later.
LATE_SHARE = 0.08
LATE_DAYS = (1, 2, 3)

# A point-day whose first delivery was wrong and is superseded by a version 2 later.
CORRECTION_SHARE = 0.03
CORRECTION_LAG_DAYS = (5, 6, 7, 8, 9)
CORRECTED_INTERVALS = (4, 12)
CORRECTION_SIGMA = 0.30

# An interval that is simply never delivered. It stays an absent row, never a zero.
MISSING_SHARE = 0.002

# A meter exchanged during the year. Modelled at local midnight, so one day has one meter.
# ponytail: a real swap happens mid-day, which would split a batch in two and change the
# staging grain to (point x interval x meter). Move the boundary if settlement ever needs it.
METER_SWAP_SHARE_PER_YEAR = 0.02
METER_LETTERS = "ABCDEFGHIJ"

# When the overnight delivery lands, Vienna time, on the day after the consumption day.
DELIVERY_TIME = time(3, 30)

MAX_LOOKBACK_DAYS = 1 + max(LATE_DAYS) + max(CORRECTION_LAG_DAYS)


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


def delay_days(point: str, day: date, seed: str) -> int:
    """How many days late this point-day arrives. Drawn from the pair, never from the clock."""
    generator = rng(seed, "delay", point, day)
    if generator.random() >= LATE_SHARE:
        return 0
    return generator.choice(LATE_DAYS)


def is_corrected(point: str, day: date, seed: str) -> bool:
    """Whether this point-day's first delivery turns out to be wrong."""
    return rng(seed, "corrected", point, day).random() < CORRECTION_SHARE


def correction_lag_days(point: str, day: date, seed: str) -> int:
    """How long after the first delivery the correction follows it."""
    return rng(seed, "correction_lag", point, day).choice(CORRECTION_LAG_DAYS)


def corrected_positions(point: str, day: date, count: int, seed: str) -> tuple[int, ...]:
    """Which intervals of the day the correction touches. The rest stay untouched."""
    generator = rng(seed, "corrected_positions", point, day)
    how_many = min(generator.randint(*CORRECTED_INTERVALS), count)
    return tuple(sorted(generator.sample(range(count), how_many)))


def is_missing(point: str, day: date, position: int, seed: str) -> bool:
    """Whether one interval is never delivered at all."""
    return rng(seed, "missing", point, day, position).random() < MISSING_SHARE


def meter_id_on(member: Member, day: date, seed: str) -> str:
    """The meter serving this point on this day, which changes when a meter is exchanged."""
    swaps = sum(
        1
        for year in range(VALID_FROM.year, day.year + 1)
        for swap in (_swap_day(member.metering_point, year, seed),)
        if swap is not None and swap <= day
    )
    return f"{member.meter_id[:-1]}{METER_LETTERS[swaps % len(METER_LETTERS)]}"


def _swap_day(point: str, year: int, seed: str) -> date | None:
    """The day a meter was exchanged at this point in this year, if it was."""
    generator = rng(seed, "meter_swap", point, year)
    if generator.random() >= METER_SWAP_SHARE_PER_YEAR:
        return None
    start = date(year, 1, 1)
    span = (date(year + 1, 1, 1) - start).days
    return start + timedelta(days=generator.randrange(span))


def meter_history(member: Member, year: int, seed: str) -> tuple[tuple[date, str], ...]:
    """Every meter this point has had through the end of `year`, with the day it took over."""
    history = [(VALID_FROM.date(), meter_id_on(member, VALID_FROM.date(), seed))]
    for candidate in range(VALID_FROM.year, year + 1):
        swap = _swap_day(member.metering_point, candidate, seed)
        if swap is not None and swap > VALID_FROM.date():
            history.append((swap, meter_id_on(member, swap, seed)))
    return tuple(history)


def _delivered_at(consumption_day: date, offset_days: int) -> datetime:
    """When a delivery lands: the morning after the consumption day, plus any delay."""
    landing = consumption_day + timedelta(days=1 + offset_days)
    return datetime.combine(landing, DELIVERY_TIME, VIENNA).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Delivery:
    """One message to send, under the key it belongs to."""

    key: str
    payload: dict[str, object]


def deliveries_for(
    run_day: date,
    members: tuple[Member, ...],
    profiles: dict[str, dict[datetime, float]],
    seed: str,
) -> tuple[Delivery, ...]:
    """Everything that arrives on one day: yesterday's values, the late ones, the corrections.

    A run does not deliver one consumption day. It delivers whatever the grid operator got
    round to sending, which is why a pipeline needs a reprocessing window rather than a
    single-day assumption.
    """
    ours = tuple(member for member in members if member.ours)
    if not ours:
        raise SimulationError("the registry holds none of our customers")

    firsts: dict[date, list[Member]] = {}
    corrections: dict[date, list[Member]] = {}
    for age in range(MAX_LOOKBACK_DAYS):
        day = run_day - timedelta(days=1 + age)
        for member in ours:
            point = member.metering_point
            delay = delay_days(point, day, seed)
            if age == delay:
                firsts.setdefault(day, []).append(member)
            elif is_corrected(point, day, seed) and age == delay + correction_lag_days(
                point, day, seed
            ):
                corrections.setdefault(day, []).append(member)

    truth = _remembered(members, profiles, seed)
    deliveries: list[Delivery] = [_community_delivery(run_day, truth)]

    for day, people in firsts.items():
        intervals, consumption, allocated, _ = truth(day)
        for member in people:
            deliveries.append(_first_delivery(member, day, intervals, consumption, allocated, seed))

    for day, people in corrections.items():
        intervals, consumption, allocated, _ = truth(day)
        for member in people:
            correction = _correction(member, day, intervals, consumption, allocated, seed)
            if correction is not None:
                deliveries.append(correction)

    # A fixed order, so two runs of the same day emit the same bytes in the same sequence.
    return tuple(
        sorted(deliveries, key=lambda d: (d.key, d.payload["delivered_at"], d.payload["version"]))
    )


def _remembered(members: tuple[Member, ...], profiles: dict[str, dict[datetime, float]], seed: str):
    """Simulate each day at most once: a run touches up to two weeks of consumption days."""
    remembered: dict[date, tuple] = {}

    def truth(day: date) -> tuple:
        if day not in remembered:
            remembered[day] = simulate_day(day, members, profiles, seed)
        return remembered[day]

    return truth


def _community_delivery(run_day: date, truth) -> Delivery:
    """The community's own totals for yesterday.

    ponytail: the community reports itself on time and never corrects. In reality the final
    allocation is only fixed by day 16; model that when phase 2 reconciles against it.
    """
    day = run_day - timedelta(days=1)
    intervals, consumption, _, generation = truth(day)
    payload = community_batch(
        1, _delivered_at(day, 0), intervals[0], generation, community_totals(consumption)
    )
    return Delivery(COMMUNITY_KEY, payload)


def _first_delivery(
    member: Member,
    day: date,
    intervals: tuple[datetime, ...],
    consumption: dict[str, tuple[float, ...]],
    allocated: dict[str, tuple[float, ...]],
    seed: str,
) -> Delivery:
    """The overnight delivery: mostly right, sometimes incomplete, sometimes wrong."""
    point = member.metering_point
    used: list[float | None] = list(consumption[point])
    given: list[float | None] = list(allocated[point])

    wrong = (
        set(corrected_positions(point, day, len(intervals), seed))
        if is_corrected(point, day, seed)
        else set()
    )
    estimates = rng(seed, "estimate", point, day)

    for position in range(len(intervals)):
        if is_missing(point, day, position, seed):
            used[position] = given[position] = None
        elif position in wrong:
            used[position] = round(
                used[position] * _unit_lognormal(estimates, CORRECTION_SIGMA), DECIMALS
            )
            given[position] = min(given[position], used[position])

    payload = reading_batch(
        point,
        meter_id_on(member, day, seed),
        1,
        _delivered_at(day, delay_days(point, day, seed)),
        intervals[0],
        used,
        given,
    )
    return Delivery(point, payload)


def _correction(
    member: Member,
    day: date,
    intervals: tuple[datetime, ...],
    consumption: dict[str, tuple[float, ...]],
    allocated: dict[str, tuple[float, ...]],
    seed: str,
) -> Delivery | None:
    """Version 2: the true values, for the intervals the first delivery got wrong.

    Everything it does not touch is `null`, so the winner rule has to keep version 1 on those
    intervals rather than blanking them. That is the case step 9 has to get right.
    """
    point = member.metering_point
    count = len(intervals)
    used: list[float | None] = [None] * count
    given: list[float | None] = [None] * count

    for position in corrected_positions(point, day, count, seed):
        if is_missing(point, day, position, seed):
            continue
        used[position] = consumption[point][position]
        given[position] = allocated[point][position]

    if all(value is None for value in used):
        return None

    offset = delay_days(point, day, seed) + correction_lag_days(point, day, seed)
    payload = reading_batch(
        point,
        meter_id_on(member, day, seed),
        2,
        _delivered_at(day, offset),
        intervals[0],
        used,
        given,
    )
    return Delivery(point, payload)


def registrations(
    members: tuple[Member, ...], year: int, seed: str
) -> dict[datetime, list[Member]]:
    """Our metering points and every meter that has served them, grouped by the day it took over."""
    grouped: dict[datetime, list[Member]] = {}
    for member in members:
        if not member.ours:
            continue
        for valid_from, meter in meter_history(member, year, seed):
            moment = datetime.combine(valid_from, time.min, VIENNA)
            grouped.setdefault(moment, []).append(replace(member, meter_id=meter))
    return grouped


def _profiles_for(run_day: date, members: tuple[Member, ...]):
    """Load every profile year a run can reach back into."""
    from megavolt.warehouse import load_profiles

    wanted = sorted({member.profile_type for member in members})
    earliest = run_day - timedelta(days=MAX_LOOKBACK_DAYS)
    profiles: dict[str, dict[datetime, float]] = {name: {} for name in wanted}
    for year in sorted({earliest.year, run_day.year}):
        for name, series in load_profiles(year, wanted).items():
            profiles[name].update(series)
    return profiles


def _delivery_day(delivery_date: date) -> tuple[str, tuple[Member, ...], tuple[Delivery, ...]]:
    """Everything one delivery day sends, computed from the seed and the stored profiles."""
    from megavolt.community import members as registry
    from megavolt.community import seed as default_seed

    seed = default_seed()
    people = registry(seed)
    profiles = _profiles_for(delivery_date, people)
    return seed, people, deliveries_for(delivery_date, people, profiles, seed)


def deliver(delivery_date: date) -> tuple[int, int]:
    """Register our metering points, then produce one delivery day into Redpanda.

    The command line and the daily DAG both call this, so a scheduled run and a hand-run day
    cannot drift apart. Registration comes first and runs every time: a reading whose point is
    not registered drops out of staging, and a repeated registration inserts nothing.
    Returns the new registry rows and the number of messages produced.
    """
    from megavolt.warehouse import store_metering_points

    seed, people, deliveries = _delivery_day(delivery_date)
    registered = sum(
        store_metering_points(group, valid_from)
        for valid_from, group in sorted(registrations(people, delivery_date.year, seed).items())
    )
    _produce(deliveries)
    return registered, len(deliveries)


def main() -> None:
    """Produce one delivery day into Redpanda, or print it and send nothing."""
    import argparse

    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delivery-date",
        required=True,
        type=date.fromisoformat,
        help="the day the messages arrive, which carries the previous day's consumption",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would be sent and send nothing"
    )
    arguments = parser.parse_args()

    if arguments.dry_run:
        _, _, deliveries = _delivery_day(arguments.delivery_date)
        _print(arguments.delivery_date, deliveries)
        return

    registered, produced = deliver(arguments.delivery_date)
    print(f"registered {registered} new metering point rows")
    print(f"produced {produced} messages to {READINGS_TOPIC}")


def _print(run_day: date, deliveries: tuple[Delivery, ...]) -> None:
    """Print a digest of one run. Identical content prints identically, which is the point."""
    firsts = [d for d in deliveries if d.payload["version"] == 1 and d.key != COMMUNITY_KEY]
    corrections = [d for d in deliveries if d.payload["version"] > 1]

    # Everything arrives today, so lateness shows in which consumption day a message carries.
    yesterday = day_intervals(run_day - timedelta(days=1))[0]
    on_time = sum(1 for d in firsts if d.payload["interval_start"] == utc_text(yesterday))

    print(f"{run_day}: {len(deliveries)} messages")
    print(
        f"  {len(firsts)} first deliveries: {on_time} for yesterday, {len(firsts) - on_time} late"
    )
    print(f"  {len(corrections)} corrections")
    print(f"  {sum(1 for d in deliveries if d.key == COMMUNITY_KEY)} community aggregate\n")
    for delivery in deliveries:
        encoded = encode(delivery.payload)
        absent = sum(1 for value in delivery.payload["consumption"] if value is None)
        print(
            f"  {delivery.key}  v{delivery.payload['version']}"
            f"  delivered {delivery.payload['delivered_at']}"
            f"  from {delivery.payload['interval_start']}"
            f"  {len(encoded):>5} bytes  {absent:>3} absent"
            f"  sha {hashlib.sha256(encoded).hexdigest()[:12]}"
        )


def _produce(deliveries: tuple[Delivery, ...]) -> None:
    """Send every message, then wait for the broker to acknowledge all of them."""
    from confluent_kafka import Producer

    failures: list[str] = []

    def report(error, message) -> None:
        if error is not None:
            failures.append(f"{message.key()!r}: {error}")

    producer = Producer(
        {"bootstrap.servers": bootstrap_servers(), "enable.idempotence": True, "acks": "all"}
    )
    for delivery in deliveries:
        producer.produce(
            READINGS_TOPIC, key=delivery.key, value=encode(delivery.payload), on_delivery=report
        )
    remaining = producer.flush(timeout=60)

    if remaining:
        raise SimulationError(f"{remaining} messages were never acknowledged")
    if failures:
        raise SimulationError(f"{len(failures)} messages failed: {failures[0]}")


if __name__ == "__main__":
    main()
