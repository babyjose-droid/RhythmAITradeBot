"""Baseline (interim) risk gate for Phase 3 paper trading and backtests.

Deterministic and independent of any LLM. Phase 4 replaces it with the full risk engine
(RISK_MANAGEMENT.md); the order flow already requires a gate decision for every order.
Limits are ILLUSTRATIVE PAPER-TEST SETTINGS, NOT RECOMMENDATIONS.

No martingale: position size depends only on equity, stop distance, caps and cash — never on
whether earlier trades won or lost. Losses can only *block* new entries (daily loss stop).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any

from rhythm.strategies.base import Signal, SignalType

VERSION = "baseline-0.1"


class RiskOutcome(StrEnum):
    ACCEPT = "ACCEPT"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class BaselineRiskLimits:
    risk_per_trade_pct: Decimal = Decimal("0.005")  # 0.5 % of equity at stop
    max_position_pct: Decimal = Decimal("0.20")  # of equity, per position
    daily_loss_stop_pct: Decimal = Decimal("0.015")  # 1.5 % of start-of-day equity
    max_open_orders: int = 10
    max_positions: int = 5
    cash_buffer_pct: Decimal = Decimal("0.003")  # slippage + charges headroom

    def describe(self) -> dict[str, Any]:
        return {k: str(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskDecision:
    outcome: RiskOutcome
    quantity: int
    checks: list[RiskCheck] = field(default_factory=list)
    version: str = VERSION

    @property
    def accepted(self) -> bool:
        return self.outcome is not RiskOutcome.BLOCK and self.quantity > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "quantity": self.quantity,
            "version": self.version,
            "checks": [asdict(c) for c in self.checks],
        }


@dataclass(frozen=True)
class AccountState:
    equity: Decimal
    cash: Decimal
    day_start_equity: Decimal
    open_orders: int
    open_positions: int
    position_quantity: int  # in the signal's instrument


def _floor_lots(qty: Decimal, lot: int) -> int:
    whole = int(qty.to_integral_value(ROUND_FLOOR))
    return max(0, whole - whole % lot)


def evaluate(
    signal: Signal,
    state: AccountState,
    *,
    data_fresh: bool,
    lot_size: int = 1,
    limits: BaselineRiskLimits | None = None,
) -> RiskDecision:
    lim = limits or BaselineRiskLimits()
    checks: list[RiskCheck] = []

    if signal.type is SignalType.EXIT_LONG:
        ok = state.position_quantity > 0
        checks.append(RiskCheck("position_exists", ok, f"qty={state.position_quantity}"))
        # Exits reduce risk and are allowed even on stale data (they fill on the next bar).
        return RiskDecision(
            RiskOutcome.ACCEPT if ok else RiskOutcome.BLOCK, state.position_quantity, checks
        )

    def block(name: str, detail: str) -> RiskDecision:
        checks.append(RiskCheck(name, False, detail))
        return RiskDecision(RiskOutcome.BLOCK, 0, checks)

    if not data_fresh:
        return block("data_fresh", "market data not FRESH")
    checks.append(RiskCheck("data_fresh", True))
    if state.position_quantity > 0:
        return block("no_pyramiding", "already holding this instrument")
    day_pnl = state.equity - state.day_start_equity
    if day_pnl <= -lim.daily_loss_stop_pct * state.day_start_equity:
        return block("daily_loss_stop", f"day P&L {day_pnl:.2f}")
    checks.append(RiskCheck("daily_loss_stop", True, f"day P&L {day_pnl:.2f}"))
    if state.open_orders >= lim.max_open_orders:
        return block("max_open_orders", str(state.open_orders))
    if state.open_positions >= lim.max_positions:
        return block("max_positions", str(state.open_positions))
    if signal.stop is None or signal.stop >= signal.ref_price:
        return block("stop_valid", "stop missing or not below entry")
    checks.append(RiskCheck("limits", True))

    per_share = signal.ref_price - signal.stop
    by_risk = _floor_lots(state.equity * lim.risk_per_trade_pct / per_share, lot_size)
    by_cap = _floor_lots(state.equity * lim.max_position_pct / signal.ref_price, lot_size)
    by_cash = _floor_lots(state.cash / (signal.ref_price * (1 + lim.cash_buffer_pct)), lot_size)
    qty = min(by_risk, by_cap, by_cash)
    checks.append(
        RiskCheck("sizing", qty > 0, f"risk={by_risk} cap={by_cap} cash={by_cash} -> {qty}")
    )
    if qty <= 0:
        return RiskDecision(RiskOutcome.BLOCK, 0, checks)
    outcome = RiskOutcome.ACCEPT if qty == by_risk else RiskOutcome.REDUCE
    return RiskDecision(outcome, qty, checks)
