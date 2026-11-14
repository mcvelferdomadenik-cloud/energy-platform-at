"""The thin door to the private pricing engine, and the guards around it.

The engine is a separate private package installed only where the tool runs. This module imports
it when first needed, so the tool starts and its tests run without it; a price is then refused
with a plain message. Nothing here knows how a price is formed: rows go in, an offer comes out.
The guards are the platform's, not the engine's: how often one person may ask, and how the
answer is rounded before it is shown or written down.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import tempfile
import threading
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from megavolt_tool.store import Customer, Store

# How many calculations one person may run in an hour. An assumption: a busy sales day is
# a few dozen; a script is thousands. Model extraction needs volume, and this is the ceiling.
CALCULATIONS_PER_HOUR = 60
# What leaves this module, on screen and in the audit record alike: prices and components to a
# hundredth of a cent, bills in whole euros.
PRICE_DECIMALS = 4
# How long a failed portfolio calculation is left alone before a page view may start it again.
# An assumption: a run loads every customer's history, so a reload must not repeat it at will.
RETRY_AFTER = timedelta(minutes=10)


class ToolError(RuntimeError):
    """Raised when the tool cannot price, with a message that can be shown as it is."""


class Engine(Protocol):
    """What the tool needs from the private package, so a test can stand in for it."""

    def price(
        self,
        prices: list,
        settled: list,
        annual_kwh: float,
        spread: float,
        contract: str,
        others: Any,
    ) -> Any: ...

    def portfolio(self, prices: list, customers: dict[str, list]) -> Any: ...


def engine() -> Engine:
    """The private engine behind the protocol; it never raises here, only when it is used."""
    return _Wrapped()


class _Wrapped:
    """The private package, imported on first use, its errors turned into the tool's own."""

    @staticmethod
    def _modules() -> tuple[Any, Any]:
        try:
            from megavolt_pricing import engine as private
            from megavolt_pricing import market
        except ImportError as exc:
            raise ToolError("the pricing engine is not installed on this machine") from exc
        return private, market

    def _rows(self, private: Any, settled: Iterable[tuple]) -> list:
        return [private.SettledQuarterHour(m, b, a) for m, b, a in settled]

    def price(self, prices, settled, annual_kwh, spread, contract, others):
        private, market = self._modules()
        points = [market.PricePoint(m, p) for m, p in prices]
        try:
            return private.price(
                points, self._rows(private, settled), annual_kwh, spread, contract, others
            )
        except ValueError as exc:
            raise ToolError(f"cannot price this customer: {exc}") from exc

    def portfolio(self, prices, customers):
        private, market = self._modules()
        points = [market.PricePoint(m, p) for m, p in prices]
        rows = {k: self._rows(private, v) for k, v in customers.items()}
        try:
            return private.portfolio(points, rows)
        except ValueError as exc:
            raise ToolError(f"cannot place the portfolio: {exc}") from exc


class RateLimit:
    """Calculations per person per rolling hour, in memory: one process, one laptop."""

    def __init__(self, per_hour: int = CALCULATIONS_PER_HOUR) -> None:
        self._per_hour = per_hour
        self._calls: dict[str, list[datetime]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, who: str, now: datetime | None = None) -> None:
        """Record one calculation for `who`, or raise if they are over the ceiling."""
        now = now or datetime.now(UTC)
        with self._lock:
            recent = [t for t in self._calls[who] if now - t < timedelta(hours=1)]
            if len(recent) >= self._per_hour:
                self._calls[who] = recent
                raise ToolError("too many calculations this hour; try again later")
            recent.append(now)
            self._calls[who] = recent


class Portfolio:
    """The portfolio's risk figures, the settlement spread and every customer's offer, computed
    once a day.

    Every customer's settled history is loaded once (350 customers, about 65k rows each over
    22 months) and the lock is held for the minutes that takes, so the app warms it in a
    background thread and the pages ask `ready()`, which never waits.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._value: Any = None
        self._offers: list[dict] = []
        self.spread: float | None = None
        self._day: date | None = None
        self.failed: str | None = None
        self.failed_at: datetime | None = None
        self._lock = threading.Lock()
        # Today's result as one JSON file, so a restart of the process reads it back in
        # milliseconds instead of pricing 350 customers again. It holds the engine's portfolio
        # risk figures, the settlement spread and the day's offer row per customer; it lives
        # under data/, which is gitignored, and only its owner may read it.
        self._cache_dir = cache_dir

    def reset(self) -> None:
        """Forget today's figures in memory (tests)."""
        with self._lock:
            self._value, self._day, self.failed, self.failed_at = None, None, None, None

    def busy(self) -> bool:
        """Whether the day's figures are being computed right now."""
        return self._lock.locked()

    def cooling(self) -> bool:
        """Whether the last calculation failed so recently that it is not started again."""
        return self.failed_at is not None and datetime.now(UTC) - self.failed_at < RETRY_AFTER

    def ready(self) -> list[dict] | None:
        """Today's offers, one row per customer, if they exist; None, without waiting, if not."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self._offers if self._known(datetime.now(UTC).date()) else None
        finally:
            self._lock.release()

    def current(self, store: Store, calculator: Engine) -> Any:
        """Today's figures, computed on first use of the day; the rows are loaded once for the
        figures, the spread and the offers."""
        with self._lock:
            today = datetime.now(UTC).date()
            if not self._known(today):
                prices, registry = store.prices(), store.customers()
                rows = {c.metering_point: store.settled(c.metering_point) for c in registry}
                spread = store.spread_eur_kwh()
                figures = calculator.portfolio(prices, rows)
                self._offers = _offers(registry, rows, prices, spread, calculator, figures)
                self._value, self.spread, self._day = figures, spread, today
                self._write(today)
            return self._value

    def _known(self, day: date) -> bool:
        """Whether the day's figures are in memory, read back from the file if it has them;
        called with the lock held."""
        if self._value is None or self._day != day:
            found = self._read(day)
            if found is None:
                return False
            (self._value, self._offers, self.spread), self._day = found, day
        return True

    def _path(self, day: date) -> Path:
        return self._cache_dir / f"{day.isoformat()}.json"

    def _read(self, day: date) -> tuple[Any, list[dict], float] | None:
        """The day's file, or None when it is missing, cut short or of an older shape."""
        try:
            data = json.loads(self._path(day).read_text(encoding="utf-8"))
            figures = SimpleNamespace(
                high_errors=tuple(data["high_errors"]),
                market_ranges=tuple(data["market_ranges"]),
                skipped=int(data["skipped"]),
            )
            return figures, list(data["offers"]), float(data["spread_eur_kwh"])
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def _write(self, day: date) -> None:
        """The day's file, complete or not there at all: written beside it, then moved over it.
        `mkstemp` creates the file readable by its owner only."""
        path = self._path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "high_errors": list(self._value.high_errors),
            "market_ranges": list(self._value.market_ranges),
            "skipped": self._value.skipped,
            "spread_eur_kwh": self.spread,
            "offers": self._offers,
        }
        handle, beside = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out)
            os.replace(beside, path)
        except OSError:
            Path(beside).unlink(missing_ok=True)
            raise


def _offers(
    registry: list[Customer],
    rows: dict[str, list],
    prices: list,
    spread: float,
    calculator: Engine,
    figures: Any,
) -> list[dict]:
    """Both offers for every customer at the volume we supply them, rounded as the screen gets
    them; a customer the engine refuses, or one without settled history, stays unpriced."""
    out = []
    for customer in registry:
        settled = rows[customer.metering_point]
        # The same average over the same settled quarter hours as the single-customer query.
        supplied = sum(float(a) for _, _, a in settled) / len(settled) * 96 * 365 if settled else 0
        row = {
            "segment": customer.segment,
            "annual_kwh": float(customer.annual_kwh),
            "supplied_kwh": round(supplied),
            "fixed_eur_kwh": None,
            "expected_bill_eur": None,
        }
        if settled:
            try:
                view = shown(calculator.price(prices, settled, supplied, spread, "both", figures))
                row["fixed_eur_kwh"] = view["fixed"]["eur_kwh"]
                row["expected_bill_eur"] = view["dynamic"]["expected_bill_eur"]
            except ToolError:
                pass
        out.append(row)
    return out


def _spread(values: list[float]) -> tuple[float, float, float] | None:
    """Lowest, median and highest, or None when there is nothing to spread."""
    return (min(values), statistics.median(values), max(values)) if values else None


def by_segment(offers: list[dict]) -> list[dict]:
    """The day's offers per segment: how many customers, how much energy, and how the fixed
    offer (c/kWh) and the expected market-linked bill (EUR a year) spread across them."""
    out = []
    for segment in sorted({o["segment"] for o in offers}):
        rows = [o for o in offers if o["segment"] == segment]
        registered = sum(o["annual_kwh"] for o in rows)
        supplied = sum(o["supplied_kwh"] for o in rows)
        fixed = [100 * o["fixed_eur_kwh"] for o in rows if o["fixed_eur_kwh"] is not None]
        out.append(
            {
                "segment": segment,
                "customers": len(rows),
                "registered_kwh": registered,
                "supplied_kwh": supplied,
                "share": 100 * supplied / registered if registered else 0.0,
                "priced": len(fixed),
                "fixed": _spread(fixed),
                "bills": _spread(
                    [o["expected_bill_eur"] for o in rows if o["expected_bill_eur"] is not None]
                ),
            }
        )
    return out


def _rounded(value: float, decimals: int | None = PRICE_DECIMALS) -> float | int:
    """A finite number rounded for the screen; anything else is refused before it is shown."""
    if not isinstance(value, int | float) or not math.isfinite(value):
        raise ToolError("the engine returned a figure that is not a number")
    return round(value, decimals) if decimals is not None else round(value)


def shown(offer: Any) -> dict[str, Any]:
    """The offer as the screen and the audit record get it: rounded, nothing beyond the contract."""
    out: dict[str, Any] = {"reasons": list(offer.reasons)}
    if offer.fixed is not None:
        out["fixed"] = {
            "eur_kwh": _rounded(offer.fixed.eur_kwh),
            "expected_eur_kwh": _rounded(offer.fixed.expected_eur_kwh),
            "market_premium_eur_kwh": _rounded(offer.fixed.market_premium_eur_kwh),
            "volume_premium_eur_kwh": _rounded(offer.fixed.volume_premium_eur_kwh),
            "margin_eur_kwh": _rounded(offer.fixed.margin_eur_kwh),
        }
    if offer.dynamic is not None:
        out["dynamic"] = {
            "expected_bill_eur": _rounded(offer.dynamic.expected_bill_eur, None),
            "high_bill_eur": _rounded(offer.dynamic.high_bill_eur, None),
            "fee_eur_kwh": _rounded(offer.dynamic.fee_eur_kwh),
        }
    return out
