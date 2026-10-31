"""Bar-based fill simulation shared by backtests and paper trading.

Assumptions (conservative, documented in BACKTESTING_METHODOLOGY.md):
* Orders decided at a bar's close can only fill from the *next* bar onward.
* Market orders fill at the next bar's open with adverse slippage, clamped to the bar range.
* Limit orders fill only if the bar trades through the limit, at the better of open/limit.
* Stop orders trigger when the bar touches the stop; gaps fill at the open (worse than stop).
* Fill quantity per bar is capped at ``max_participation`` x bar volume (partial fills).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any

from rhythm.execution.costs import Side
from rhythm.marketdata.types import Bar


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"  # stop-market


@dataclass(frozen=True)
class FillQuote:
    quantity: int
    price: Decimal


def _to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    return (price / tick).quantize(Decimal(1), rounding=rounding) * tick


@dataclass(frozen=True)
class FillModel:
    slippage_bps: Decimal = Decimal(5)
    max_participation: Decimal = Decimal("0.10")

    def describe(self) -> dict[str, Any]:
        return {k: str(v) for k, v in asdict(self).items()}

    def _slip(self, price: Decimal, side: Side, tick: Decimal, bar: Bar) -> Decimal:
        factor = self.slippage_bps / Decimal(10_000)
        if side is Side.BUY:
            return min(_to_tick(price * (1 + factor), tick, up=True), bar.high)
        return max(_to_tick(price * (1 - factor), tick, up=False), bar.low)

    def available(self, bar: Bar) -> int:
        return int((Decimal(bar.volume) * self.max_participation).to_integral_value(ROUND_FLOOR))

    def simulate(
        self,
        *,
        side: Side,
        order_type: OrderType,
        remaining: int,
        bar: Bar,
        tick: Decimal,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        capacity: int | None = None,
    ) -> FillQuote | None:
        """Fill (if any) for one order against one bar. ``capacity`` = volume left in bar."""
        cap = self.available(bar) if capacity is None else capacity
        qty = min(remaining, cap)
        if qty <= 0:
            return None
        if order_type is OrderType.MARKET:
            return FillQuote(qty, self._slip(bar.open, side, tick, bar))
        if order_type is OrderType.LIMIT:
            if limit_price is None:
                raise ValueError("limit order without limit price")
            if side is Side.BUY and bar.low <= limit_price:
                return FillQuote(qty, min(bar.open, limit_price))
            if side is Side.SELL and bar.high >= limit_price:
                return FillQuote(qty, max(bar.open, limit_price))
            return None
        if stop_price is None:
            raise ValueError("stop order without stop price")
        if side is Side.SELL and bar.low <= stop_price:
            return FillQuote(qty, self._slip(min(bar.open, stop_price), side, tick, bar))
        if side is Side.BUY and bar.high >= stop_price:
            return FillQuote(qty, self._slip(max(bar.open, stop_price), side, tick, bar))
        return None
