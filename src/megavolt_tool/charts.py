"""Small SVG charts drawn on the server: no chart library, nothing the browser has to trust.

Three forms only. Columns for a day's shape (24 hourly values, one series), horizontal bars for
a handful of yearly amounts side by side, and range bars for a spread per group. Marks are
thin, data-ends rounded, labels sparse, and every mark carries a `<title>` so hovering reads
the exact value.

No viewBox: the horizontal axis is in percent of the card and the vertical one in pixels, so a
chart narrows on a phone while its text keeps its size instead of shrinking with the drawing.
"""

from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from markupsafe import Markup

VIENNA = ZoneInfo("Europe/Vienna")
# The brand violet and the orange of the reference palette: far apart in hue and lightness, so
# the two contracts stay apart for every kind of colour vision.
BLUE, ORANGE, ORANGE_SOFT = "#6250d6", "#eb6834", "#f6b89d"
INK, MUTED, LINE, GRID = "#16151f", "#6b6a78", "#e6e5ef", "#d6d5e2"
# A label at the edge may stick out of the drawing instead of being cut off.
SHOW = 'overflow="visible"'


def day_profile(settled: list[tuple[datetime, float, float]]) -> list[float]:
    """Average kWh taken per local hour over the settled history: the customer's typical day."""
    taken: dict[int, list[float]] = defaultdict(list)
    for when, _bought, actual in settled:
        taken[when.astimezone(VIENNA).hour].append(actual)
    return [sum(taken[h]) / len(taken[h]) if taken[h] else 0.0 for h in range(24)]


def _text(x: str, y: float, text: str, size: int, fill: str, anchor: str = "start") -> str:
    """One line, or one tspan per line when the label has a line break (month over year)."""
    spans = "".join(
        f'<tspan x="{x}" dy="{12 if i else 0}">{escape(line)}</tspan>'
        for i, line in enumerate(text.split("\n"))
    )
    return (
        f'<text x="{x}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size}" fill="{fill}">'
        f"{spans}</text>"
    )


def _rule(y: float, dashed: bool = False) -> str:
    dash = ' stroke-dasharray="2 4"' if dashed else ""
    return f'<line x1="0" y1="{y:.1f}" x2="100%" y2="{y:.1f}" stroke="{LINE}"{dash}/>'


def monthly_bills(
    settled: list[tuple[datetime, float, float]],
    prices: list[tuple[datetime, float]],
    fixed_eur_kwh: float | None = None,
    fee_eur_kwh: float = 0.0,
) -> list[tuple[str, float]]:
    """What the last twelve settled months would have cost: at a fixed price, or at the market
    price of each quarter hour plus the fee. Plain arithmetic over public prices and the
    customer's own history; nothing of the engine."""
    price_at = {when: eur_mwh / 1000 for when, eur_mwh in prices}
    bills: dict[tuple[int, int], float] = defaultdict(float)
    days: dict[tuple[int, int], set] = defaultdict(set)
    for when, _bought, actual in settled:
        local = when.astimezone(VIENNA)
        unit_price = fixed_eur_kwh if fixed_eur_kwh is not None else price_at.get(when)
        if unit_price is None:
            continue
        month = (local.year, local.month)
        bills[month] += actual * (unit_price + (0 if fixed_eur_kwh else fee_eur_kwh))
        days[month].add(local.date())
    if not bills:
        return []
    # Twelve calendar months up to the newest; a month with fewer than 20 settled days shows as
    # empty rather than as a misleadingly small bar. The year is written under January and
    # under the first month, so both years are always on the page.
    year, month = max(bills)
    out = []
    for i in range(12):
        full = len(days[year, month]) >= 20
        name = calendar.month_abbr[month] + (f"\n{year % 100:02d}" if month == 1 or i == 11 else "")
        out.append((name, bills[year, month] if full else 0.0))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return out[::-1]


def _step(peak: float) -> float:
    """A gridline step from 1, 2, 5 x 10^n so that two lines fit under the peak."""
    raw = peak / 2.5
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return next(m * magnitude for m in (5, 2, 1) if m * magnitude <= raw) if raw > 0 else 1


def _amount(value: float, unit: str) -> str:
    """Whole euros; energy to two decimals below 100 kWh. No false precision on a chart."""
    if unit == "€" or value >= 100:
        return f"{value:,.0f} {unit}"
    return f"{value:.2f} {unit}"


def columns(
    values: list[float], unit: str, color: str = BLUE, labels: list[str] | None = None
) -> Markup:
    """Columns on a baseline, the largest one labelled; hours 0/6/12/18 or the given labels.
    A zero with a label is "no data": an empty outline instead of a bar."""
    height, top, bottom, left = 200, 16, 40 if labels else 28, 64
    peak = max(values) or 1.0
    slot = 100 / len(values)
    plot = height - top - bottom
    parts = [
        f'<svg width="100%" height="{height}" {SHOW} role="img" aria-label="columns">',
        f'<svg x="{left}" style="width:calc(100% - {left}px)" height="{height}" {SHOW}>',
        _rule(height - bottom),
    ]
    axis = []
    step = _step(peak)
    for tick in (step, 2 * step):
        if tick >= peak:
            break
        y = height - bottom - plot * tick / peak
        parts.append(_rule(y, dashed=True))
        axis.append(_text(f"{left - 8}", y + 4, _amount(tick, unit), 11, MUTED, "end"))
    biggest = values.index(peak) if peak in values else -1
    for i, value in enumerate(values):
        h = plot * value / peak
        x, w = f"{slot * (i + 0.1):.2f}%", f"{slot * 0.8:.2f}%"
        mid = f"{slot * (i + 0.5):.2f}%"
        y = height - bottom - h
        name = labels[i] if labels is not None else f"{i:02d}:00"
        title = escape(name.replace("\n", " "))
        if value == 0 and labels is not None:
            parts.append(
                f'<rect x="{x}" y="{height - bottom - plot * 0.25:.1f}" width="{w}"'
                f' height="{plot * 0.25:.1f}" rx="4" fill="none" stroke="{GRID}"'
                f' stroke-dasharray="3 3"><title>{title} · no data</title></rect>'
            )
        else:
            parts.append(
                f'<rect x="{x}" y="{y:.1f}" width="{w}" height="{h:.1f}" rx="4"'
                f' fill="{color}"><title>{title} · {_amount(value, unit)}</title></rect>'
            )
        if i == biggest:
            parts.append(_text(mid, y - 6, _amount(value, unit), 12, INK, "middle"))
        if labels is not None or i % 6 == 0:
            size = 10 if labels else 11
            parts.append(_text(mid, height - bottom + 16, name, size, MUTED, "middle"))
    parts += ["</svg>", *axis, "</svg>"]
    return Markup("".join(parts))  # noqa: S704 - every value above went through escape()


def bars(items: list[tuple[str, float, str, float | None]], unit: str) -> Markup:
    """Yearly amounts as horizontal bars: (name, value, colour, high) each; a high draws the
    range up to it as a soft tail, so a contract with a spread shows as one and the other does
    not, without either looking better. The name and the amount sit above each bar, so the bars
    can use the whole width on any screen."""
    row = 48
    peak = max(high or value for _, value, _, high in items) or 1.0
    height = row * len(items)
    parts = [f'<svg width="100%" height="{height}" {SHOW} role="img" aria-label="a year">']
    for i, (name, value, color, high) in enumerate(items):
        y = i * row
        w = 100 * value / peak
        if high and high > value:
            parts.append(
                f'<rect x="{w:.2f}%" y="{y + 22}" width="{100 * (high - value) / peak:.2f}%"'
                f' height="20" rx="4" fill="{ORANGE_SOFT}" stroke="{color}" stroke-dasharray="3 3"'
                f"><title>{escape(name)} · up to {_amount(high, unit)}</title></rect>"
            )
        parts.append(
            f'<rect x="0" y="{y + 22}" width="{w:.2f}%" height="20" rx="4" fill="{color}">'
            f"<title>{escape(name)} · {_amount(value, unit)}</title></rect>"
        )
        amount = _amount(value, unit) + (f" expected · up to {_amount(high, unit)}" if high else "")
        parts.append(
            f'<text x="0" y="{y + 14}" font-size="13"><tspan fill="{MUTED}">{escape(name)}</tspan>'
            f'<tspan fill="{INK}" dx="10">{escape(amount)}</tspan></text>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - every value above went through escape()


def ranges(items: list[tuple[str, float, float, float]], unit: str) -> Markup:
    """A spread per group as horizontal range bars on one scale from zero: (name, lowest,
    median, highest) each; the band runs from lowest to highest and a tick marks the median.
    The name and the three figures sit above each bar and in the drawing's own description."""
    row = 48
    peak = max(high for _, _, _, high in items) or 1.0
    said = "; ".join(
        f"{name} {_amount(low, unit)} to {_amount(high, unit)}, median {_amount(mid, unit)}"
        for name, low, mid, high in items
    )
    parts = [
        f'<svg width="100%" height="{row * len(items)}" {SHOW} role="img"'
        f' aria-label="{escape(said)}"><title>{escape(said)}</title>'
    ]
    for i, (name, low, mid, high) in enumerate(items):
        y = i * row
        span = f"{_amount(low, unit)} to {_amount(high, unit)}"
        figures = [_amount(v, "").strip() for v in (low, high, mid)]
        short = "{} – {} {} · median {}".format(*figures[:2], unit, figures[2])
        parts.append(
            f'<rect x="0" y="{y + 30}" width="100%" height="4" rx="2" fill="{LINE}"/>'
            f'<rect x="{100 * low / peak:.2f}%" y="{y + 22}" height="20" rx="4" fill="{BLUE}"'
            f' width="{100 * (high - low) / peak:.2f}%" fill-opacity="0.35" stroke="{BLUE}">'
            f"<title>{escape(name)} · {span}</title></rect>"
            f'<svg x="{100 * mid / peak:.2f}%" y="{y + 18}" {SHOW}><rect x="-2" width="4"'
            f' height="28" rx="2" fill="{INK}"><title>{escape(name)} · median'
            f" {_amount(mid, unit)}</title></rect></svg>"
            f'<text x="0" y="{y + 14}" font-size="13"><tspan fill="{MUTED}">{escape(name)}</tspan>'
            f'<tspan fill="{INK}" dx="10">{escape(short)}</tspan>'
            "</text>"
        )
    parts.append("</svg>")
    return Markup("".join(parts))  # noqa: S704 - every value above went through escape()
