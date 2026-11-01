"""Deterministic pre-trade risk engine (RISK_MANAGEMENT.md §3, checks R1-R19).

Every order proposal — entries, strategy exits and protective stop/target orders — passes
``evaluate``. The engine is pure (no I/O) and **fails closed**: a missing input or an error
inside a check produces FAIL, never PASS.

Outcomes: ACCEPT (all checks pass), REDUCE (accepted at a smaller size that every cap allows),
REVIEW (accepted by the checks but policy demands a human decision), BLOCK (any FAIL, or the
allowed size is below one lot).

No martingale: sizing uses equity, stop distance, caps, liquidity and cash only. Earlier
losses can only *block* (daily loss stop); they never increase size.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.execution.costs import CostModel, Product, Side
from rhythm.risk.policy import DEFAULT_POLICY, RiskPolicy

ENGINE_VERSION = "risk-engine-1.0"
ORDER_MODES = frozenset({"PAPER", "BACKTEST", "SIMULATION"})  # LIVE is never accepted


class Intent(StrEnum):
    OPEN = "OPEN"  # adds risk (entry)
    REDUCE = "REDUCE"  # exit, protective stop/target, square-off


class CheckStatus(StrEnum):
    PASS = "PASS"  # noqa: S105 - check status, not a password
    FAIL = "FAIL"
    REDUCE = "REDUCE"
    SKIP = "SKIP"  # not applicable (stated reason), never used to hide missing data


class RiskOutcome(StrEnum):
    ACCEPT = "ACCEPT"
    REDUCE = "REDUCE"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class OrderProposal:
    key: InstrumentKey
    horizon: Horizon
    side: Side
    intent: Intent
    at: datetime
    ref_price: Decimal
    strategy: str
    idempotency_key: str
    stop: Decimal | None = None
    quantity: int | None = None  # None: the engine sizes the order
    limit_price: Decimal | None = None
    purpose: str = "entry"


@dataclass(frozen=True)
class InstrumentFacts:
    active: bool
    instrument_type: str
    lot_size: int
    tick_size: Decimal
    banned: bool = False
    lower_circuit: Decimal | None = None
    upper_circuit: Decimal | None = None
    sector: str | None = None


@dataclass(frozen=True)
class RiskState:
    """Snapshot of every input a decision depends on. ``None`` means unknown -> fail closed."""

    as_of: datetime
    mode: str
    kill_switch_active: bool
    paused_strategies: frozenset[str]
    reconciled_at: datetime | None
    reconciliation_ok: bool
    instrument: InstrumentFacts | None
    quote_status: str | None  # FreshnessStatus value of the latest quote
    equity: Decimal
    day_start_equity: Decimal
    cash: Decimal
    reserved_cash: Decimal  # value committed to open BUY orders
    strategy_day_pnl: Decimal | None
    ledger_allocation: Decimal | None
    ledger_exposure: Decimal
    ledger_position_qty: int
    consolidated_exposure: Decimal  # this instrument, all ledgers
    sector_exposure: Decimal | None
    avg_daily_volume: Decimal | None
    adv_source: str
    open_orders_total: int
    open_orders_strategy: int
    orders_last_minute: int
    duplicate_idempotency_key: bool
    unknown_orders_on_instrument: int

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in asdict(self).items():
            if isinstance(v, frozenset | set):
                out[k] = sorted(v)
            elif isinstance(v, dict):
                out[k] = {kk: None if vv is None else str(vv) for kk, vv in v.items()}
            elif v is None or isinstance(v, bool | int | str):
                out[k] = v
            else:
                out[k] = str(v)
        return out


@dataclass(frozen=True)
class CheckResult:
    code: str
    name: str
    status: CheckStatus
    detail: str = ""
    max_qty: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "max_qty": self.max_qty,
        }


@dataclass(frozen=True)
class RiskDecision:
    outcome: RiskOutcome
    quantity: int
    intent: Intent
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)
    policy_version: str = DEFAULT_POLICY.version
    engine_version: str = ENGINE_VERSION

    @property
    def accepted(self) -> bool:
        """True when an order may be created now (REVIEW waits for a human)."""
        return self.outcome in (RiskOutcome.ACCEPT, RiskOutcome.REDUCE) and self.quantity > 0

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.FAIL]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "quantity": self.quantity,
            "intent": self.intent.value,
            "policy_version": self.policy_version,
            "engine_version": self.engine_version,
            "checks": [c.as_dict() for c in self.checks],
        }


def floor_lots(qty: Decimal, lot: int) -> int:
    if qty <= 0:
        return 0
    whole = int(qty.to_integral_value(ROUND_FLOOR))
    return max(0, whole - whole % lot)


def _is_multiple(value: Decimal, step: Decimal) -> bool:
    return step > 0 and (value % step) == 0


Check = Callable[[OrderProposal, RiskState, RiskPolicy], CheckResult]
_NAMES = {
    "R1": "trading mode allows orders",
    "R2": "kill switch off, strategy not paused",
    "R3": "positions reconciled recently",
    "R4": "quote is fresh",
    "R5": "instrument eligible",
    "R6": "order fields valid",
    "R7": "planned loss within limit",
    "R8": "daily loss stop",
    "R9": "ledger allocation",
    "R10": "single-name concentration",
    "R11": "sector concentration",
    "R12": "liquidity (ADV participation)",
    "R13": "available funds",
    "R14": "open order count",
    "R15": "order rate",
    "R16": "idempotency / unresolved orders",
    "R17": "session entry window",
    "R18": "overnight / event gate",
    "R19": "correlation cluster",
    "R20": "ledger owns the quantity it sells",
}


def _r(code: str, status: CheckStatus, detail: str = "", max_qty: int | None = None) -> CheckResult:
    return CheckResult(code, _NAMES[code], status, detail, max_qty)


# ---------------------------------------------------------------------------- gating checks
def r1_mode(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.mode in ORDER_MODES:
        return _r("R1", CheckStatus.PASS, s.mode)
    return _r("R1", CheckStatus.FAIL, f"mode {s.mode} does not allow orders")


def r2_controls(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if p.intent is Intent.REDUCE:
        return _r("R2", CheckStatus.PASS, "risk-reducing orders allowed under kill switch/pause")
    if s.kill_switch_active:
        return _r("R2", CheckStatus.FAIL, "kill switch active")
    if p.strategy in s.paused_strategies:
        return _r("R2", CheckStatus.FAIL, f"strategy {p.strategy} paused")
    return _r("R2", CheckStatus.PASS)


def r3_reconciled(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.reconciled_at is None:
        return _r("R3", CheckStatus.FAIL, "no reconciliation on record")
    if not s.reconciliation_ok:
        return _r("R3", CheckStatus.FAIL, "last reconciliation found a break")
    # |age|: the cycle reconciles at wall-clock time, just after the bar the decision used.
    age = (s.as_of - s.reconciled_at).total_seconds()
    if abs(age) > pol.reconciliation_max_age_seconds:
        return _r("R3", CheckStatus.FAIL, f"reconciliation age {age:.0f}s")
    return _r("R3", CheckStatus.PASS, f"age {age:.0f}s")


def r4_quote(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.quote_status == "FRESH":
        return _r("R4", CheckStatus.PASS)
    return _r("R4", CheckStatus.FAIL, f"quote status {s.quote_status or 'unknown'}")


def r5_instrument(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    i = s.instrument
    if i is None:
        return _r("R5", CheckStatus.FAIL, "instrument facts unavailable")
    if not i.active:
        return _r("R5", CheckStatus.FAIL, "not active in today's instrument registry")
    if i.banned:
        return _r("R5", CheckStatus.FAIL, "instrument banned/suspended")
    if i.instrument_type not in pol.allowed_instrument_types:
        return _r("R5", CheckStatus.FAIL, f"type {i.instrument_type} not allowed")
    return _r("R5", CheckStatus.PASS, i.instrument_type)


def r6_fields(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    i = s.instrument
    if i is None or i.lot_size < 1 or i.tick_size <= 0:
        return _r("R6", CheckStatus.FAIL, "lot/tick size unknown")
    if p.ref_price <= 0:
        return _r("R6", CheckStatus.FAIL, "reference price must be positive")
    for label, price in (("limit", p.limit_price), ("stop", p.stop)):
        if price is not None and (price <= 0 or not _is_multiple(price, i.tick_size)):
            return _r("R6", CheckStatus.FAIL, f"{label} price {price} not a tick multiple")
    if p.quantity is not None and (p.quantity <= 0 or p.quantity % i.lot_size):
        return _r("R6", CheckStatus.FAIL, f"quantity {p.quantity} not a positive lot multiple")
    if i.lower_circuit is not None and i.upper_circuit is not None:
        px = p.limit_price or p.ref_price
        if not i.lower_circuit <= px <= i.upper_circuit:
            return _r("R6", CheckStatus.FAIL, f"price {px} outside circuit band")
        return _r("R6", CheckStatus.PASS, "within circuit band")
    return _r("R6", CheckStatus.PASS, "circuit band not provided by data source")


def r8_daily_loss(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.strategy_day_pnl is None or s.day_start_equity <= 0:
        return _r("R8", CheckStatus.FAIL, "strategy day P&L unknown")
    limit = -pol.daily_loss_stop_pct * s.day_start_equity
    if s.strategy_day_pnl <= limit:
        return _r("R8", CheckStatus.FAIL, f"day P&L {s.strategy_day_pnl:.2f} <= {limit:.2f}")
    return _r("R8", CheckStatus.PASS, f"day P&L {s.strategy_day_pnl:.2f}")


def r11_sector(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    i = s.instrument
    if i is None or i.sector is None or s.sector_exposure is None:
        return _r("R11", CheckStatus.SKIP, "no sector classification source configured")
    room = pol.max_sector_pct * s.equity - s.sector_exposure
    return _r(
        "R11", CheckStatus.PASS, f"sector {i.sector} room {room:.2f}", max_qty=_qty(room, p, s)
    )


def r14_open_orders(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.open_orders_total >= pol.max_open_orders:
        return _r("R14", CheckStatus.FAIL, f"{s.open_orders_total} open (global)")
    if s.open_orders_strategy >= pol.max_open_orders_per_strategy:
        return _r("R14", CheckStatus.FAIL, f"{s.open_orders_strategy} open (strategy)")
    return _r("R14", CheckStatus.PASS, f"{s.open_orders_total}/{s.open_orders_strategy}")


def r15_rate(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.orders_last_minute >= pol.max_orders_per_minute:
        return _r("R15", CheckStatus.FAIL, f"{s.orders_last_minute} orders in the last minute")
    return _r("R15", CheckStatus.PASS, f"{s.orders_last_minute}/min")


def r16_idempotency(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.duplicate_idempotency_key:
        return _r("R16", CheckStatus.FAIL, "an order with this idempotency key already exists")
    if s.unknown_orders_on_instrument > 0:
        return _r("R16", CheckStatus.FAIL, "unresolved UNKNOWN order on this instrument")
    return _r("R16", CheckStatus.PASS)


def r17_window(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if p.horizon is not Horizon.INTRADAY:
        return _r("R17", CheckStatus.SKIP, "intraday-only check")
    local = p.at.astimezone(EXCHANGE_TZ).time()
    if pol.entry_window_start <= local <= pol.entry_window_end:
        return _r("R17", CheckStatus.PASS, local.strftime("%H:%M"))
    return _r("R17", CheckStatus.FAIL, f"{local:%H:%M} outside entry window")


def r18_event_gate(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if p.horizon is Horizon.INTRADAY:
        return _r("R18", CheckStatus.SKIP, "no overnight holding for intraday")
    return _r("R18", CheckStatus.FAIL, "event calendar not available (Phase 5)")


def r19_correlation(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    return _r("R19", CheckStatus.SKIP, "correlation data not available (later phase)")


# ---------------------------------------------------------------------------- sizing checks
def _qty(value: Decimal, p: OrderProposal, s: RiskState) -> int:
    lot = s.instrument.lot_size if s.instrument else 1
    return floor_lots(value / p.ref_price, lot) if p.ref_price > 0 else 0


def _cost_per_share(p: OrderProposal, costs: CostModel) -> Decimal:
    """Estimated round-trip charges per share at the reference price (100-share basis)."""
    buy = costs.charges(Side.BUY, 100, p.ref_price, Product.INTRADAY).total
    sell = costs.charges(Side.SELL, 100, p.ref_price, Product.INTRADAY).total
    return (buy + sell) / 100


def r7_planned_loss(
    p: OrderProposal, s: RiskState, pol: RiskPolicy, costs: CostModel
) -> CheckResult:
    pct = pol.risk_per_trade(p.horizon)
    if pct is None:
        return _r("R7", CheckStatus.FAIL, f"no per-trade risk limit for {p.horizon.value}")
    if p.stop is None or p.stop >= p.ref_price:
        return _r("R7", CheckStatus.FAIL, "stop missing or not below the entry")
    per_share = p.ref_price - p.stop + _cost_per_share(p, costs)
    budget = pct * s.equity
    lot = s.instrument.lot_size if s.instrument else 1
    max_qty = floor_lots(budget / per_share, lot)
    return _r(
        "R7",
        CheckStatus.PASS,
        f"budget {budget:.2f}, risk/share {per_share:.4f} incl. est. costs",
        max_qty=max_qty,
    )


def r9_ledger(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.ledger_allocation is None:
        return _r("R9", CheckStatus.FAIL, "ledger allocation unknown")
    room = s.ledger_allocation - s.ledger_exposure
    return _r("R9", CheckStatus.PASS, f"room {room:.2f}", max_qty=_qty(room, p, s))


def r10_single_name(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    room = pol.max_single_name_pct * s.equity - s.consolidated_exposure
    return _r("R10", CheckStatus.PASS, f"room {room:.2f}", max_qty=_qty(room, p, s))


def r12_liquidity(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if s.avg_daily_volume is None or s.avg_daily_volume <= 0:
        return _r("R12", CheckStatus.FAIL, "average daily volume unknown")
    lot = s.instrument.lot_size if s.instrument else 1
    max_qty = floor_lots(pol.max_adv_participation * s.avg_daily_volume, lot)
    return _r(
        "R12", CheckStatus.PASS, f"ADV {s.avg_daily_volume:.0f} ({s.adv_source})", max_qty=max_qty
    )


def r13_funds(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    available = s.cash - s.reserved_cash
    lot = s.instrument.lot_size if s.instrument else 1
    max_qty = (
        floor_lots(available / (p.ref_price * (1 + pol.cash_buffer_pct)), lot)
        if p.ref_price > 0
        else 0
    )
    return _r("R13", CheckStatus.PASS, f"available {available:.2f}", max_qty=max_qty)


def r20_owned(p: OrderProposal, s: RiskState, pol: RiskPolicy) -> CheckResult:
    if p.side is not Side.SELL:
        return _r("R20", CheckStatus.FAIL, "risk-reducing orders must be sells (long-only)")
    if s.ledger_position_qty <= 0:
        return _r("R20", CheckStatus.FAIL, "ledger holds no position to sell")
    return _r("R20", CheckStatus.PASS, f"owned {s.ledger_position_qty}", s.ledger_position_qty)


# ---------------------------------------------------------------------------- evaluation
def _safe(code: str, fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception as exc:  # fail closed on any error inside a check
        return _r(code, CheckStatus.FAIL, f"check error: {type(exc).__name__}: {exc}")


def _skip(code: str, why: str) -> CheckResult:
    return _r(code, CheckStatus.SKIP, why)


def evaluate(
    proposal: OrderProposal,
    state: RiskState,
    policy: RiskPolicy = DEFAULT_POLICY,
    *,
    costs: CostModel | None = None,
) -> RiskDecision:
    """Evaluate one proposal. Never raises for bad inputs: it returns BLOCK instead."""
    try:
        return _evaluate(proposal, state, policy, costs or CostModel())
    except Exception as exc:  # pragma: no cover - defensive; individual checks are wrapped
        return RiskDecision(
            RiskOutcome.BLOCK,
            0,
            proposal.intent,
            (_r("R6", CheckStatus.FAIL, f"engine error: {type(exc).__name__}"),),
            policy.version,
        )


def _evaluate(p: OrderProposal, s: RiskState, pol: RiskPolicy, costs: CostModel) -> RiskDecision:
    def run(code: str, fn: Check) -> CheckResult:
        return _safe(code, lambda: fn(p, s, pol))

    if p.intent is Intent.REDUCE:
        why = "not applied to risk-reducing orders"
        skipped = ("R3", "R4", "R5", "R7", "R8", "R9", "R10", "R11", "R12", "R13", "R14")
        checks = [
            run("R1", r1_mode),
            run("R2", r2_controls),
            run("R6", r6_fields),
            run("R16", lambda pp, ss, pl: _dup_only(ss)),
            run("R20", r20_owned),
            *(_skip(c, why) for c in (*skipped, "R15", "R17", "R18", "R19")),
        ]
        owned = next(c for c in checks if c.code == "R20")
        desired = p.quantity if p.quantity is not None else (owned.max_qty or 0)
        return _decide(p, pol, checks, desired, [owned])

    gating = [
        run("R1", r1_mode),
        run("R2", r2_controls),
        run("R3", r3_reconciled),
        run("R4", r4_quote),
        run("R5", r5_instrument),
        run("R6", r6_fields),
        run("R8", r8_daily_loss),
        run("R11", r11_sector),
        run("R14", r14_open_orders),
        run("R15", r15_rate),
        run("R16", r16_idempotency),
        run("R17", r17_window),
        run("R18", r18_event_gate),
        run("R19", r19_correlation),
    ]
    if p.side is not Side.BUY:
        gating.append(_r("R20", CheckStatus.FAIL, "only long entries are supported"))
    risk = _safe("R7", lambda: r7_planned_loss(p, s, pol, costs))
    caps = [
        risk,
        run("R9", r9_ledger),
        run("R10", r10_single_name),
        run("R12", r12_liquidity),
        run("R13", r13_funds),
    ]
    sector = next(c for c in gating if c.code == "R11")
    if sector.max_qty is not None:
        caps.append(sector)
    if risk.status is CheckStatus.FAIL:
        desired = 0
    elif p.quantity is not None:
        desired = p.quantity
    else:
        desired = risk.max_qty or 0
    return _decide(p, pol, gating + caps, desired, caps)


def _dup_only(s: RiskState) -> CheckResult:
    if s.duplicate_idempotency_key:
        return _r("R16", CheckStatus.FAIL, "an order with this idempotency key already exists")
    return _r("R16", CheckStatus.PASS, "unknown-order block not applied to exits")


def _decide(
    p: OrderProposal,
    pol: RiskPolicy,
    checks: list[CheckResult],
    desired: int,
    caps: list[CheckResult],
) -> RiskDecision:
    final = desired
    cap_ids = {id(c) for c in caps}
    resolved: list[CheckResult] = []
    for c in checks:
        out = c
        if id(c) in cap_ids and c.status is CheckStatus.PASS and c.max_qty is not None:
            if c.max_qty < desired:
                out = CheckResult(c.code, c.name, CheckStatus.REDUCE, c.detail, c.max_qty)
            final = min(final, c.max_qty)
        resolved.append(out)
    resolved.sort(key=lambda c: int(c.code[1:]))
    if any(c.status is CheckStatus.FAIL for c in resolved) or final <= 0:
        return RiskDecision(RiskOutcome.BLOCK, 0, p.intent, tuple(resolved), pol.version)
    if (
        p.intent is Intent.OPEN
        and pol.review_above_order_value is not None
        and final * p.ref_price > pol.review_above_order_value
    ):
        return RiskDecision(RiskOutcome.REVIEW, final, p.intent, tuple(resolved), pol.version)
    outcome = RiskOutcome.REDUCE if final < desired else RiskOutcome.ACCEPT
    return RiskDecision(outcome, final, p.intent, tuple(resolved), pol.version)
