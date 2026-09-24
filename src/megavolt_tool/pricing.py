"""The thin door to the private pricing engine, and the guards around it.

The engine is a separate private package installed only where the tool runs. This module imports
it when first needed, so the tool starts and its tests run without it; a price is then refused
with a plain message. Nothing here knows how a price is formed: rows go in, an offer comes out.
The guards are the platform's, not the engine's: how often one person may ask, and how the
answer is rounded before it is shown or written down.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from megavolt_tool.store import Store

# How many calculations one person may run in an hour. An assumption: a busy sales day is
# a few dozen; a script is thousands. Model extraction needs volume, and this is the ceiling.
CALCULATIONS_PER_HOUR = 60
# What leaves this module, on screen and in the audit record alike: prices and components to a
# hundredth of a cent, bills in whole euros.
PRICE_DECIMALS = 4


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
    """The portfolio's risk figures, computed once a day over every customer with enough history.

    ponytail: every customer's settled year is loaded at once (350 x 35k rows) and the lock is
    held for the minutes that takes; warm it in a background thread if that ever hurts.
    """

    def __init__(self) -> None:
        self._value: Any = None
        self._day: date | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Forget today's figures (tests, and a manual refresh)."""
        with self._lock:
            self._value, self._day = None, None

    def current(self, store: Store, calculator: Engine) -> Any:
        """Today's figures, computed on first use of the day."""
        with self._lock:
            today = datetime.now(UTC).date()
            if self._value is None or self._day != today:
                prices = store.prices()
                customers = {
                    c.metering_point: store.settled(c.metering_point) for c in store.customers()
                }
                self._value = calculator.portfolio(prices, customers)
                self._day = today
            return self._value


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
