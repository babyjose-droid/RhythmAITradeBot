"""Swing baseline: trend pullback on daily bars (long only, multi-day holding).

A deliberately simple, explainable *baseline* for exercising the swing pipeline. Not a claim
of edge. Parameters are frozen per version.

Evaluated at each completed daily bar (decision at that session's close):

* **Trend:** close > SMA(slow) and SMA(fast) > SMA(slow).
* **Entry** (no position, trend intact): today's close crosses back above SMA(fast) after at
  least one close at/below it within the last ``pullback_lookback`` sessions. No entry when a
  known corporate event is within ``avoid_event_days`` (the risk engine's R18 also blocks).
* **Stop** = lowest low of the last ``stop_lookback`` sessions; skipped when the stop is more
  than ``max_stop_pct`` (or less than ``min_stop_pct``) away. **Target** = entry +
  ``reward_risk`` x risk.
* **Thesis** recorded with the entry: why it was taken and what would invalidate it.
* **Exits** (position open): thesis invalidated — close below SMA(slow); time stop after
  ``max_holding_sessions``; exit before a known event within ``exit_before_event_days``.
  Stop and target are protective orders kept at the venue across days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon
from rhythm.marketdata.types import Bar
from rhythm.strategies.base import (
    Evaluation,
    EventView,
    Signal,
    SignalType,
    StrategyContext,
    dataclass_params,
)


@dataclass(frozen=True)
class SwingParams:
    sma_fast: int = 20
    sma_slow: int = 50
    pullback_lookback: int = 5
    stop_lookback: int = 10
    min_stop_pct: Decimal = Decimal("0.01")
    max_stop_pct: Decimal = Decimal("0.08")
    reward_risk: Decimal = Decimal("2")
    max_holding_sessions: int = 20
    avoid_event_days: int = 5
    exit_before_event_days: int = 1


def sma(values: list[Decimal], n: int) -> Decimal | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:], Decimal(0)) / n


def _local_day(dt: datetime) -> date:
    return dt.astimezone(EXCHANGE_TZ).date()


class SwingTrendPullback:
    """Daily trend-pullback entries with an explicit, checkable thesis; long only."""

    name = "swing_trend_pullback"
    version = "1.0.0"
    horizon = Horizon.SWING

    def __init__(self, params: SwingParams | None = None) -> None:
        self._p = params or SwingParams()

    @property
    def params(self) -> dict[str, Any]:
        return dataclass_params(self._p)

    @property
    def min_history(self) -> int:
        return self._p.sma_slow + 1

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        p = self._p
        bars = list(ctx.bars)
        diag: dict[str, Any] = {"bars": len(bars)}
        if len(bars) < self.min_history:
            diag["reason"] = f"need {self.min_history} daily bars, have {len(bars)}"
            return Evaluation(diagnostics=diag)
        closes = [b.close for b in bars]
        last = bars[-1]
        today = _local_day(ctx.now)
        fast, slow = sma(closes, p.sma_fast), sma(closes, p.sma_slow)
        prev_fast = sma(closes[:-1], p.sma_fast)
        if fast is None or slow is None or prev_fast is None:  # pragma: no cover - guarded above
            return Evaluation(diagnostics=diag)
        diag |= {
            "close": str(last.close),
            "sma_fast": str(round(fast, 4)),
            "sma_slow": str(round(slow, 4)),
        }
        nxt = self._next_event(ctx.events, today)
        days_to_event = None if nxt is None else (nxt.event_date - today).days
        diag["days_to_next_event"] = days_to_event

        if ctx.position is not None and ctx.position.quantity > 0:
            return self._exits(ctx, bars, slow, days_to_event, diag)

        trend = last.close > slow and fast > slow
        crossed = last.close > fast and bars[-2].close <= prev_fast
        window = bars[-(p.pullback_lookback + 1) : -1]
        fasts = [
            sma(closes[: len(closes) - i], p.sma_fast) for i in range(p.pullback_lookback, 0, -1)
        ]
        pulled_back = any(
            f is not None and b.close <= f for b, f in zip(window, fasts, strict=False)
        )
        lows = [b.low for b in bars[-p.stop_lookback :]]
        stop = min(lows)
        risk = last.close - stop
        stop_pct = risk / last.close if last.close > 0 else Decimal(0)
        checks = {
            "trend": trend,
            "cross_above_fast": crossed,
            "pullback_seen": pulled_back,
            "stop_distance_ok": p.min_stop_pct <= stop_pct <= p.max_stop_pct,
            "no_event_soon": days_to_event is None or days_to_event > p.avoid_event_days,
            "no_entry_today": ctx.entries_today == 0,
        }
        diag["checks"] = checks
        diag["stop_pct"] = str(round(stop_pct, 4))
        if not all(checks.values()):
            diag["reason"] = "entry conditions not met"
            return Evaluation(diagnostics=diag)
        target = last.close + p.reward_risk * risk
        thesis = {
            "rationale": [
                f"uptrend: close {last.close} > SMA{p.sma_slow} {round(slow, 2)}, "
                f"SMA{p.sma_fast} > SMA{p.sma_slow}",
                f"pullback to SMA{p.sma_fast} within {p.pullback_lookback} sessions, "
                f"closed back above it ({round(fast, 2)})",
            ],
            "invalidation": [
                {"rule": "close_below_sma", "window": p.sma_slow},
                {"rule": "stop", "price": str(stop)},
                {"rule": "max_holding_sessions", "value": p.max_holding_sessions},
                {"rule": "exit_before_event_days", "value": p.exit_before_event_days},
            ],
            "review_every_sessions": 1,
        }
        return Evaluation(
            [
                Signal(
                    key=ctx.key,
                    horizon=self.horizon,
                    type=SignalType.ENTER_LONG,
                    at=ctx.now,
                    ref_price=last.close,
                    strategy=self.name,
                    version=self.version,
                    stop=stop,
                    target=target,
                    reasons=("trend pullback entry",),
                    thesis=thesis,
                )
            ],
            diag,
        )

    def _exits(
        self,
        ctx: StrategyContext,
        bars: list[Bar],
        slow: Decimal,
        days_to_event: int | None,
        diag: dict[str, Any],
    ) -> Evaluation:
        p = self._p
        last = bars[-1]
        opened = _local_day(ctx.position.opened_at) if ctx.position else _local_day(ctx.now)
        held = sum(1 for b in bars if _local_day(b.start) > opened)
        reasons: list[str] = []
        if last.close < slow:
            reasons.append(f"thesis invalidated: close {last.close} below SMA{p.sma_slow}")
        if held >= p.max_holding_sessions:
            reasons.append(f"time stop: held {held} sessions")
        if days_to_event is not None and days_to_event <= p.exit_before_event_days:
            reasons.append(f"event risk: known event in {days_to_event} day(s)")
        diag |= {"held_sessions": held, "exit_reasons": reasons}
        if not reasons:
            return Evaluation(diagnostics=diag)
        return Evaluation(
            [
                Signal(
                    key=ctx.key,
                    horizon=self.horizon,
                    type=SignalType.EXIT_LONG,
                    at=ctx.now,
                    ref_price=last.close,
                    strategy=self.name,
                    version=self.version,
                    reasons=tuple(reasons),
                )
            ],
            diag,
        )

    @staticmethod
    def _next_event(events: tuple[EventView, ...], today: date) -> EventView | None:
        upcoming = sorted((e for e in events if e.event_date >= today), key=lambda e: e.event_date)
        return upcoming[0] if upcoming else None
