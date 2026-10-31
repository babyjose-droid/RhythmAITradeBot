"""Intraday baseline: opening-range breakout with a VWAP filter (long only).

This is a deliberately simple, explainable *baseline* for testing the pipeline. It is not a
claim of edge. Parameters are frozen per version; any change requires a new version.

Rules (evaluated at the close of each completed 5-minute bar, IST session):
* Opening range (OR) = high/low of bars starting in the first ``opening_range_minutes``.
* Entry (no position, no entry yet today, inside the entry window): close > OR high,
  close > session VWAP, and bar volume >= ``volume_multiple`` x median volume of the
  earlier session bars. Stop = OR low. Skip if stop distance > ``max_stop_pct`` of price.
  Target = entry + ``reward_risk`` x stop distance.
* Exit (position open): close < VWAP (if ``vwap_exit``), or time >= ``exit_time``.
  Stop and target are protective orders handled by the execution layer.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from typing import Any

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon
from rhythm.strategies.base import (
    Evaluation,
    Signal,
    SignalType,
    StrategyContext,
    dataclass_params,
    session_vwap,
)


@dataclass(frozen=True)
class OrbParams:
    opening_range_minutes: int = 15
    entry_start: time = time(9, 30)
    entry_cutoff: time = time(14, 45)
    exit_time: time = time(15, 5)
    volume_multiple: Decimal = Decimal("1.0")
    min_prior_bars: int = 3
    max_stop_pct: Decimal = Decimal("0.02")
    reward_risk: Decimal = Decimal("2")
    vwap_exit: bool = True
    max_entries_per_day: int = 1


class IntradayOrbVwap:
    """Opening-range breakout with VWAP and volume filters; long only; flat by session end."""

    name = "intraday_orb_vwap"
    version = "1.0.0"
    horizon = Horizon.INTRADAY

    def __init__(self, params: OrbParams | None = None) -> None:
        self._p = params or OrbParams()

    @property
    def params(self) -> dict[str, Any]:
        d = dataclass_params(self._p)
        return {k: (v.isoformat() if isinstance(v, time) else v) for k, v in d.items()}

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        p = self._p
        now_local = ctx.now.astimezone(EXCHANGE_TZ)
        today = now_local.date()
        session = [b for b in ctx.bars if b.start.astimezone(EXCHANGE_TZ).date() == today]
        diag: dict[str, Any] = {"bars_today": len(session)}
        if not session:
            diag["reason"] = "no session bars"
            return Evaluation(diagnostics=diag)
        last = session[-1]
        vwap = session_vwap(session)
        diag["vwap"] = None if vwap is None else str(round(vwap, 4))
        t = now_local.time()

        # ---------------------------------------------------------------- exits
        if ctx.position is not None and ctx.position.quantity > 0:
            reason = None
            if t >= p.exit_time:
                reason = "time_exit"
            elif p.vwap_exit and vwap is not None and last.close < vwap:
                reason = "vwap_exit"
            if reason:
                return Evaluation(
                    [self._signal(ctx, SignalType.EXIT_LONG, last.close, reasons=(reason,))], diag
                )
            diag["reason"] = "holding"
            return Evaluation(diagnostics=diag)

        # ---------------------------------------------------------------- entries
        if ctx.entries_today >= p.max_entries_per_day:
            diag["reason"] = "max entries reached"
            return Evaluation(diagnostics=diag)
        if not (p.entry_start <= t <= p.entry_cutoff):
            diag["reason"] = "outside entry window"
            return Evaluation(diagnostics=diag)
        open_dt = now_local.replace(hour=9, minute=15, second=0, microsecond=0)
        or_end = open_dt + timedelta(minutes=p.opening_range_minutes)
        or_bars = [b for b in session if b.start.astimezone(EXCHANGE_TZ) < or_end]
        rest = [b for b in session if b.start.astimezone(EXCHANGE_TZ) >= or_end]
        if not or_bars or not rest or or_bars[0].start.astimezone(EXCHANGE_TZ) != open_dt:
            diag["reason"] = "opening range incomplete"
            return Evaluation(diagnostics=diag)
        or_high = max(b.high for b in or_bars)
        or_low = min(b.low for b in or_bars)
        diag.update(or_high=str(or_high), or_low=str(or_low))
        prior = session[:-1]
        if len(prior) < p.min_prior_bars:
            diag["reason"] = "not enough prior bars"
            return Evaluation(diagnostics=diag)
        median_vol = Decimal(str(statistics.median(b.volume for b in prior)))
        checks = {
            "breakout": last.close > or_high,
            "above_vwap": vwap is not None and last.close > vwap,
            "volume": Decimal(last.volume) >= p.volume_multiple * median_vol,
        }
        diag["checks"] = checks
        if not all(checks.values()):
            diag["reason"] = "entry conditions not met"
            return Evaluation(diagnostics=diag)
        risk = last.close - or_low
        if risk <= 0 or risk / last.close > p.max_stop_pct:
            diag["reason"] = "stop distance outside limits"
            return Evaluation(diagnostics=diag)
        target = last.close + p.reward_risk * risk
        sig = self._signal(
            ctx,
            SignalType.ENTER_LONG,
            last.close,
            stop=or_low,
            target=target,
            reasons=("or_breakout", "above_vwap", "volume_confirmed"),
        )
        return Evaluation([sig], diag)

    def _signal(
        self,
        ctx: StrategyContext,
        kind: SignalType,
        ref: Decimal,
        *,
        stop: Decimal | None = None,
        target: Decimal | None = None,
        reasons: tuple[str, ...] = (),
    ) -> Signal:
        return Signal(
            key=ctx.key,
            horizon=self.horizon,
            type=kind,
            at=ctx.now,
            ref_price=ref,
            strategy=self.name,
            version=self.version,
            stop=stop,
            target=target,
            reasons=reasons,
        )


STRATEGIES: dict[str, type[IntradayOrbVwap]] = {IntradayOrbVwap.name: IntradayOrbVwap}
