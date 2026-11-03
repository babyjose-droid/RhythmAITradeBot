"""Long-term baseline: quality at a reasonable price, held within an allocation band.

An explainable, rules-only *baseline* for exercising the long-term pipeline. It is not a
claim of edge, and the only fundamentals source today is MOCK (licensed data: decision D-03).

Evidence model (all from fundamentals published at decision time, with provenance):
TTM EPS, ROE, debt/equity, TTM operating cash flow, YoY TTM EPS growth, P/E at the last close.

* **Quality:** ROE >= ``min_roe``, debt/equity <= ``max_de``, TTM operating cash flow > 0,
  EPS growth >= ``min_eps_growth``.
* **Entry** (no position): quality passes, P/E <= ``max_entry_pe``, fundamentals not older
  than ``max_fundamentals_age_days``, no known event within ``avoid_event_days``. Target =
  ``target_weight`` of the ledger's allocated capital.
* **Thesis invalidated** (exit everything): ROE < ``exit_roe``, TTM operating cash flow <= 0,
  or EPS growth < ``exit_eps_growth``.
* **Review** every ``review_every_sessions`` sessions, and whenever new results are
  published. On review: P/E above ``expensive_pe`` -> reduce to ``min_weight``; weight above
  ``max_weight`` -> reduce to target; weight below ``min_weight`` with the thesis intact and
  P/E acceptable -> add back to target. Otherwise hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon
from rhythm.fundamentals.types import Derived, FundamentalSnapshot, derive
from rhythm.strategies.base import (
    Evaluation,
    EventView,
    Signal,
    SignalType,
    StrategyContext,
    dataclass_params,
)


@dataclass(frozen=True)
class LongTermParams:
    min_roe: Decimal = Decimal("0.15")
    max_de: Decimal = Decimal("1.0")
    min_eps_growth: Decimal = Decimal("0")
    max_entry_pe: Decimal = Decimal("30")
    expensive_pe: Decimal = Decimal("45")
    exit_roe: Decimal = Decimal("0.10")
    exit_eps_growth: Decimal = Decimal("-0.20")
    target_weight: Decimal = Decimal("0.10")
    min_weight: Decimal = Decimal("0.05")
    max_weight: Decimal = Decimal("0.15")
    review_every_sessions: int = 20
    min_quarters: int = 8
    max_fundamentals_age_days: int = 200
    avoid_event_days: int = 5


def _day(dt: datetime) -> date:
    return dt.astimezone(EXCHANGE_TZ).date()


def _val(d: dict[str, Derived], name: str) -> Decimal | None:
    x = d.get(name)
    return None if x is None else x.value


class LongTermQualityValue:
    """Quality-at-a-reasonable-price with allocation bands and scheduled reviews; long only."""

    name = "long_term_quality_value"
    version = "1.0.0"
    horizon = Horizon.LONG_TERM

    def __init__(self, params: LongTermParams | None = None) -> None:
        self._p = params or LongTermParams()

    @property
    def params(self) -> dict[str, Any]:
        return dataclass_params(self._p)

    def evaluate(self, ctx: StrategyContext) -> Evaluation:
        p = self._p
        bars = list(ctx.bars)
        diag: dict[str, Any] = {"bars": len(bars), "quarters": len(ctx.fundamentals)}
        if not bars:
            diag["reason"] = "no price data"
            return Evaluation(diagnostics=diag)
        quarters: list[FundamentalSnapshot] = list(ctx.fundamentals)
        if len(quarters) < p.min_quarters:
            diag["reason"] = f"need {p.min_quarters} published quarters, have {len(quarters)}"
            return Evaluation(diagnostics=diag)
        last = bars[-1]
        price = last.close
        latest = quarters[-1]
        derived = derive(quarters, price)
        roe, de = _val(derived, "roe"), _val(derived, "debt_to_equity")
        ocf, growth = _val(derived, "ocf_ttm"), _val(derived, "eps_growth_yoy")
        pe = _val(derived, "pe")
        age_days = (ctx.now - latest.known_at).days
        fresh = age_days <= p.max_fundamentals_age_days
        today = _day(ctx.now)
        nxt = self._next_event(ctx.events, today)
        days_to_event = None if nxt is None else (nxt.event_date - today).days
        no_event_soon = days_to_event is None or days_to_event > p.avoid_event_days
        held_qty = ctx.position.quantity if ctx.position else 0
        weight = held_qty * price / ctx.capital if ctx.capital and ctx.capital > 0 else None
        prev_end = ctx.bar_end(bars[-2]) if len(bars) >= 2 else None
        new_results = prev_end is not None and latest.known_at > prev_end
        held_sessions = (
            sum(1 for b in bars if _day(b.start) > _day(ctx.position.opened_at))
            if ctx.position
            else 0
        )
        review_due = held_qty > 0 and (
            new_results or (held_sessions > 0 and held_sessions % p.review_every_sessions == 0)
        )
        quality = {
            "roe_ok": roe is not None and roe >= p.min_roe,
            "debt_ok": de is not None and de <= p.max_de,
            "cash_flow_ok": ocf is not None and ocf > 0,
            "growth_ok": growth is not None and growth >= p.min_eps_growth,
        }
        broken = [
            msg
            for cond, msg in (
                (roe is None or roe < p.exit_roe, f"ROE below {p.exit_roe:.0%}"),
                (ocf is None or ocf <= 0, "TTM operating cash flow not positive"),
                (
                    growth is None or growth < p.exit_eps_growth,
                    f"EPS growth below {p.exit_eps_growth:.0%}",
                ),
            )
            if cond
        ]
        diag |= {
            "price": str(price),
            "latest_period": latest.period_end.isoformat(),
            "latest_published": latest.known_at.isoformat(),
            "fundamentals_age_days": age_days,
            "fundamentals_source": latest.source,
            "metrics": {k: v.as_dict() for k, v in derived.items()},
            "quality": quality,
            "weight": None if weight is None else str(round(weight, 4)),
            "band": [str(p.min_weight), str(p.target_weight), str(p.max_weight)],
            "days_to_next_event": days_to_event,
            "held_sessions": held_sessions,
            "reviewed": review_due,
            "new_results": new_results,
        }
        if weight is None:
            diag["reason"] = "ledger capital unknown: cannot size"
            return Evaluation(diagnostics=diag)

        def sig(
            kind: SignalType, target: Decimal | None, reasons: list[str], **kw: Any
        ) -> Evaluation:
            diag["action"] = kind.value
            return Evaluation(
                [
                    Signal(
                        key=ctx.key,
                        horizon=self.horizon,
                        type=kind,
                        at=ctx.now,
                        ref_price=price,
                        strategy=self.name,
                        version=self.version,
                        reasons=tuple(reasons),
                        target_weight=target,
                        **kw,
                    )
                ],
                diag,
            )

        if held_qty <= 0:
            ok = all(quality.values()) and pe is not None and pe <= p.max_entry_pe
            diag["entry_checks"] = {
                "quality": all(quality.values()),
                "valuation_ok": pe is not None and pe <= p.max_entry_pe,
                "fundamentals_fresh": fresh,
                "no_event_soon": no_event_soon,
            }
            if not (ok and fresh and no_event_soon and ctx.entries_today == 0):
                diag["reason"] = "entry conditions not met"
                return Evaluation(diagnostics=diag)
            thesis = {
                "rationale": [
                    f"quality: ROE {roe:.1%}, debt/equity {de:.2f}, TTM operating cash flow "
                    f"positive, EPS growth {growth:.1%}",
                    f"valuation: P/E {pe:.1f} <= {p.max_entry_pe}",
                    f"evidence: {len(quarters)} published quarters, latest "
                    f"{latest.period_end.isoformat()} ({latest.source})",
                ],
                "invalidation": [
                    {"rule": "roe_below", "value": str(p.exit_roe)},
                    {"rule": "ocf_ttm_not_positive"},
                    {"rule": "eps_growth_below", "value": str(p.exit_eps_growth)},
                ],
                "allocation_band": {
                    "min": str(p.min_weight),
                    "target": str(p.target_weight),
                    "max": str(p.max_weight),
                },
                "review_every_sessions": p.review_every_sessions,
                "key_metrics": {k: v.as_dict() for k, v in derived.items()},
                "evidence": [latest.provenance("eps"), latest.provenance("total_equity")],
            }
            return sig(
                SignalType.ENTER_LONG,
                p.target_weight,
                ["quality at a reasonable price"],
                thesis=thesis,
            )

        if broken:
            return sig(
                SignalType.EXIT_LONG, Decimal(0), [f"thesis invalidated: {b}" for b in broken]
            )
        if not review_due:
            return Evaluation(diagnostics=diag)
        if pe is not None and pe > p.expensive_pe and weight > p.min_weight:
            return sig(
                SignalType.REDUCE_LONG,
                p.min_weight,
                [f"review: valuation stretched (P/E {pe:.1f} > {p.expensive_pe})"],
            )
        if weight > p.max_weight:
            return sig(
                SignalType.REDUCE_LONG,
                p.target_weight,
                [f"review: weight {weight:.1%} above band max {p.max_weight:.0%}"],
            )
        if (
            weight < p.min_weight
            and all(quality.values())
            and pe is not None
            and pe <= p.max_entry_pe
            and fresh
            and no_event_soon
        ):
            return sig(
                SignalType.ADD_LONG,
                p.target_weight,
                [f"review: weight {weight:.1%} below band min {p.min_weight:.0%}"],
            )
        diag["action"] = "HOLD"
        return Evaluation(diagnostics=diag)

    @staticmethod
    def _next_event(events: tuple[EventView, ...], today: date) -> EventView | None:
        upcoming = sorted((e for e in events if e.event_date >= today), key=lambda e: e.event_date)
        return upcoming[0] if upcoming else None
