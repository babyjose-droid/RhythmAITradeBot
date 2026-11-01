"""Risk engine: every check (R1-R20), boundaries, fail-closed behaviour and sizing properties."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Horizon
from rhythm.execution.costs import CostModel, Side
from rhythm.risk import engine
from rhythm.risk.engine import (
    CheckStatus,
    InstrumentFacts,
    Intent,
    OrderProposal,
    RiskOutcome,
    RiskState,
    evaluate,
)
from rhythm.risk.policy import DEFAULT_POLICY, PolicyValidationError, RiskPolicy
from tests.phase3_helpers import KEY

AT = datetime(2026, 10, 30, 10, 0, tzinfo=EXCHANGE_TZ)
EQ = Decimal(1_000_000)
FACTS = InstrumentFacts(active=True, instrument_type="EQ", lot_size=1, tick_size=Decimal("0.05"))


def prop(**kw: object) -> OrderProposal:
    base: dict[str, object] = {
        "key": KEY,
        "horizon": Horizon.INTRADAY,
        "side": Side.BUY,
        "intent": Intent.OPEN,
        "at": AT,
        "ref_price": Decimal(100),
        "strategy": "s",
        "idempotency_key": "k1",
        "stop": Decimal(98),
    }
    base.update(kw)
    return OrderProposal(**base)  # type: ignore[arg-type]


def state(**kw: object) -> RiskState:
    base: dict[str, object] = {
        "as_of": AT,
        "mode": "PAPER",
        "kill_switch_active": False,
        "paused_strategies": frozenset(),
        "reconciled_at": AT - timedelta(seconds=5),
        "reconciliation_ok": True,
        "instrument": FACTS,
        "quote_status": "FRESH",
        "equity": EQ,
        "day_start_equity": EQ,
        "cash": EQ,
        "reserved_cash": Decimal(0),
        "strategy_day_pnl": Decimal(0),
        "ledger_allocation": Decimal(300_000),
        "ledger_exposure": Decimal(0),
        "ledger_position_qty": 0,
        "consolidated_exposure": Decimal(0),
        "sector_exposure": None,
        "avg_daily_volume": Decimal(10_000_000),
        "adv_source": "test",
        "open_orders_total": 0,
        "open_orders_strategy": 0,
        "orders_last_minute": 0,
        "duplicate_idempotency_key": False,
        "unknown_orders_on_instrument": 0,
    }
    base.update(kw)
    return RiskState(**base)  # type: ignore[arg-type]


def check(d: engine.RiskDecision, code: str) -> engine.CheckResult:
    return next(c for c in d.checks if c.code == code)


# ---------------------------------------------------------------------------- sizing
def test_accept_sized_by_planned_loss_including_costs() -> None:
    wide = replace(DEFAULT_POLICY, version="t", max_single_name_pct=Decimal("0.5"))
    d = evaluate(prop(stop=Decimal(97)), state(ledger_allocation=EQ), wide)
    assert d.outcome is RiskOutcome.ACCEPT
    # 5000 budget / (3 + est. round-trip costs per share): below the 1666 that ignores costs
    cps = engine._cost_per_share(prop(), CostModel())
    assert d.quantity == int(Decimal(5000) / (Decimal(3) + cps))
    assert 1400 < d.quantity < 1666
    assert {c.code for c in d.checks} >= {f"R{i}" for i in range(1, 20)}


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        ({"ledger_allocation": Decimal(50_000)}, "R9"),
        ({"consolidated_exposure": Decimal(95_000)}, "R10"),
        ({"avg_daily_volume": Decimal(50_000)}, "R12"),
        ({"cash": Decimal(20_000)}, "R13"),
        ({"reserved_cash": Decimal(990_000)}, "R13"),
    ],
)
def test_caps_reduce_size(kw: dict[str, object], code: str) -> None:
    d = evaluate(prop(stop=Decimal(99)), state(**kw))  # risk alone would allow ~4000
    c = check(d, code)
    assert c.status is CheckStatus.REDUCE and d.quantity == c.max_qty
    assert d.outcome is RiskOutcome.REDUCE


def test_reduce_to_zero_blocks() -> None:
    d = evaluate(prop(), state(ledger_exposure=Decimal(300_000)))
    assert d.outcome is RiskOutcome.BLOCK and d.quantity == 0 and not d.accepted


def test_lot_size_rounding() -> None:
    lot = replace(FACTS, lot_size=50)
    d = evaluate(prop(stop=Decimal(97)), state(instrument=lot))
    assert d.quantity % 50 == 0 and d.quantity > 0


def test_requested_quantity_respected_or_reduced() -> None:
    assert evaluate(prop(quantity=10), state()).quantity == 10
    d = evaluate(prop(quantity=100_000), state())
    assert d.outcome is RiskOutcome.REDUCE and d.quantity < 100_000


# ---------------------------------------------------------------------------- each gate
@pytest.mark.parametrize(
    ("p_kw", "s_kw", "code"),
    [
        ({}, {"mode": "RESEARCH_ONLY"}, "R1"),
        ({}, {"mode": "LIVE"}, "R1"),
        ({}, {"kill_switch_active": True}, "R2"),
        ({}, {"paused_strategies": frozenset({"s"})}, "R2"),
        ({}, {"reconciled_at": None}, "R3"),
        ({}, {"reconciliation_ok": False}, "R3"),
        ({}, {"reconciled_at": AT - timedelta(seconds=61)}, "R3"),
        ({}, {"quote_status": "STALE"}, "R4"),
        ({}, {"quote_status": None}, "R4"),
        ({}, {"quote_status": "CATCH_UP"}, "R4"),
        ({}, {"instrument": None}, "R5"),
        ({}, {"instrument": replace(FACTS, active=False)}, "R5"),
        ({}, {"instrument": replace(FACTS, banned=True)}, "R5"),
        ({}, {"instrument": replace(FACTS, instrument_type="FUT")}, "R5"),
        ({"stop": Decimal("98.03")}, {}, "R6"),
        ({"quantity": 0}, {}, "R6"),
        ({"ref_price": Decimal(0)}, {}, "R6"),
        (
            {},
            {"instrument": replace(FACTS, lower_circuit=Decimal(101), upper_circuit=Decimal(120))},
            "R6",
        ),
        ({"stop": None}, {}, "R7"),
        ({"stop": Decimal(101)}, {}, "R7"),
        ({"horizon": Horizon.LONG_TERM}, {}, "R7"),
        ({}, {"strategy_day_pnl": Decimal(-15_000)}, "R8"),
        ({}, {"strategy_day_pnl": None}, "R8"),
        ({}, {"ledger_allocation": None}, "R9"),
        ({}, {"avg_daily_volume": None}, "R12"),
        ({}, {"open_orders_total": 10}, "R14"),
        ({}, {"open_orders_strategy": 5}, "R14"),
        ({}, {"orders_last_minute": 10}, "R15"),
        ({}, {"duplicate_idempotency_key": True}, "R16"),
        ({}, {"unknown_orders_on_instrument": 1}, "R16"),
        ({"at": datetime(2026, 10, 30, 9, 19, tzinfo=EXCHANGE_TZ)}, {}, "R17"),
        ({"at": datetime(2026, 10, 30, 14, 46, tzinfo=EXCHANGE_TZ)}, {}, "R17"),
        ({"horizon": Horizon.SWING}, {}, "R18"),
        ({"side": Side.SELL}, {}, "R20"),
    ],
)
def test_each_rule_blocks(p_kw: dict[str, object], s_kw: dict[str, object], code: str) -> None:
    d = evaluate(prop(**p_kw), state(**s_kw))
    assert d.outcome is RiskOutcome.BLOCK and d.quantity == 0
    assert check(d, code).status is CheckStatus.FAIL


@pytest.mark.parametrize(
    ("s_kw", "code"),
    [
        ({"reconciled_at": AT - timedelta(seconds=60)}, "R3"),  # boundary inclusive
        ({"strategy_day_pnl": Decimal("-14999.99")}, "R8"),
        ({"open_orders_total": 9}, "R14"),
        ({"open_orders_strategy": 4}, "R14"),
        ({"orders_last_minute": 9}, "R15"),
        (
            {"instrument": replace(FACTS, lower_circuit=Decimal(90), upper_circuit=Decimal(110))},
            "R6",
        ),
    ],
)
def test_boundaries_pass(s_kw: dict[str, object], code: str) -> None:
    d = evaluate(prop(), state(**s_kw))
    assert check(d, code).status is CheckStatus.PASS and d.accepted


@pytest.mark.parametrize("hhmm", [time(9, 20), time(14, 45)])
def test_entry_window_inclusive(hhmm: time) -> None:
    at = datetime.combine(AT.date(), hhmm, tzinfo=EXCHANGE_TZ)
    d = evaluate(prop(at=at), state(as_of=at, reconciled_at=at))
    assert check(d, "R17").status is CheckStatus.PASS


def test_daily_loss_boundary_blocks_exactly_at_limit() -> None:
    d = evaluate(prop(), state(strategy_day_pnl=Decimal(-15_000)))
    assert check(d, "R8").status is CheckStatus.FAIL


def test_not_applicable_checks_are_skipped_with_reasons() -> None:
    d = evaluate(prop(), state())
    for code in ("R11", "R18", "R19"):
        c = check(d, code)
        assert c.status is CheckStatus.SKIP and c.detail


def test_sector_check_used_when_data_present() -> None:
    f = replace(FACTS, sector="BANKS")
    d = evaluate(prop(stop=Decimal(99)), state(instrument=f, sector_exposure=Decimal(240_000)))
    c = check(d, "R11")
    assert c.status is CheckStatus.REDUCE and d.quantity == c.max_qty == 100


# ---------------------------------------------------------------------------- reducing orders
def test_exits_allowed_under_kill_switch_pause_stale_data_and_daily_loss() -> None:
    p = prop(side=Side.SELL, intent=Intent.REDUCE, stop=None, purpose="exit")
    s = state(
        kill_switch_active=True,
        paused_strategies=frozenset({"s"}),
        quote_status="STALE",
        reconciliation_ok=False,
        strategy_day_pnl=Decimal(-100_000),
        ledger_position_qty=70,
        unknown_orders_on_instrument=1,
    )
    d = evaluate(p, s)
    assert d.outcome is RiskOutcome.ACCEPT and d.quantity == 70


def test_exit_cannot_sell_more_than_ledger_owns() -> None:
    p = prop(side=Side.SELL, intent=Intent.REDUCE, quantity=100, stop=None)
    d = evaluate(p, state(ledger_position_qty=40))
    assert d.outcome is RiskOutcome.REDUCE and d.quantity == 40
    assert evaluate(p, state(ledger_position_qty=0)).outcome is RiskOutcome.BLOCK


def test_exit_still_blocked_in_live_or_research_mode_and_on_duplicates() -> None:
    p = prop(side=Side.SELL, intent=Intent.REDUCE, stop=None)
    for s in (
        state(mode="LIVE", ledger_position_qty=5),
        state(mode="RESEARCH_ONLY", ledger_position_qty=5),
        state(duplicate_idempotency_key=True, ledger_position_qty=5),
    ):
        assert evaluate(p, s).outcome is RiskOutcome.BLOCK


# ---------------------------------------------------------------------------- fail closed
def test_error_inside_a_check_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> engine.CheckResult:
        raise ZeroDivisionError("boom")

    monkeypatch.setattr(engine, "r4_quote", boom)
    d = evaluate(prop(), state())
    assert d.outcome is RiskOutcome.BLOCK
    assert "check error" in check(d, "R4").detail


def test_error_in_sizing_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "r12_liquidity", lambda *_: 1 / 0)
    assert evaluate(prop(), state()).outcome is RiskOutcome.BLOCK


def test_review_threshold() -> None:
    pol = replace(DEFAULT_POLICY, version="t", review_above_order_value=Decimal(10_000))
    d = evaluate(prop(), state(), pol)
    assert d.outcome is RiskOutcome.REVIEW and not d.accepted and d.quantity > 0


def test_decision_serialises() -> None:
    d = evaluate(prop(), state())
    out = d.as_dict()
    assert out["policy_version"] == DEFAULT_POLICY.version and len(out["checks"]) == 19
    assert state().as_dict()["instrument"]["lot_size"] == "1"


# ---------------------------------------------------------------------------- properties
@settings(max_examples=150, deadline=None)
@given(
    stop_gap=st.decimals(min_value="0.05", max_value="20", places=2),
    cash=st.decimals(min_value=0, max_value=2_000_000, places=0),
    alloc=st.decimals(min_value=0, max_value=1_000_000, places=0),
    adv=st.decimals(min_value=1, max_value=50_000_000, places=0),
    exposure=st.decimals(min_value=0, max_value=200_000, places=0),
    lot=st.sampled_from([1, 5, 25, 100]),
)
def test_size_never_exceeds_any_cap(
    stop_gap: Decimal, cash: Decimal, alloc: Decimal, adv: Decimal, exposure: Decimal, lot: int
) -> None:
    p = prop(stop=Decimal(100) - stop_gap)
    s = state(
        cash=cash,
        ledger_allocation=alloc,
        avg_daily_volume=adv,
        consolidated_exposure=exposure,
        instrument=replace(FACTS, lot_size=lot),
    )
    d = evaluate(p, s)
    assert d.quantity % lot == 0 and d.quantity >= 0
    assert d.quantity * stop_gap <= DEFAULT_POLICY.intraday_risk_per_trade_pct * EQ
    assert d.quantity * Decimal(100) <= alloc
    assert d.quantity * Decimal(100) <= Decimal("0.10") * EQ - exposure or d.quantity == 0
    assert d.quantity <= DEFAULT_POLICY.max_adv_participation * adv
    assert d.quantity * Decimal(100) <= cash


@settings(max_examples=100, deadline=None)
@given(loss=st.decimals(min_value=0, max_value="14999", places=2))
def test_no_martingale_losses_never_increase_size(loss: Decimal) -> None:
    flat = evaluate(prop(), state()).quantity
    after = evaluate(
        prop(),
        state(equity=EQ - loss, cash=EQ - loss, strategy_day_pnl=-loss),
    ).quantity
    assert after <= flat


# ---------------------------------------------------------------------------- policy
def test_policy_round_trip_and_versioning() -> None:
    params = DEFAULT_POLICY.to_params()
    again = RiskPolicy.from_params("x-1", params)
    assert again.to_params() == params and again.version == "x-1"


@pytest.mark.parametrize(
    "bad",
    [
        {"intraday_risk_per_trade_pct": "0.5"},
        {"daily_loss_stop_pct": "-1"},
        {"max_open_orders": 0},
        {"max_open_orders_per_strategy": 50, "max_open_orders": 10},
        {"entry_window_start": "15:00"},
        {"allowed_instrument_types": []},
        {"ledger_allocation_pct": {"INTRADAY": "0.6", "SWING": "0.3", "LONG_TERM": "0.4"}},
        {"unknown_field": 1},
        {"max_orders_per_minute": "ten"},
        {"max_orders_per_minute": 2.5},
    ],
)
def test_policy_rejects_out_of_bounds(bad: dict[str, object]) -> None:
    with pytest.raises(PolicyValidationError):
        RiskPolicy.from_params("bad", DEFAULT_POLICY.to_params() | bad)
