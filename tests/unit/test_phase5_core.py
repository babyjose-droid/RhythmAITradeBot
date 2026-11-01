"""Phase 5 core: swing strategy, event calendar, R18, swing runner, swing backtests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from rhythm.backtest.engine import BacktestConfig, BacktestInputError, run_backtest
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import DataOrigin, Exchange, Horizon, InstrumentKey
from rhythm.events.mock import ANNOUNCE_DAYS, MockEventProvider
from rhythm.events.types import CorporateEvent, EventKind, known_upcoming
from rhythm.execution.book import PaperBook
from rhythm.execution.costs import CostModel, Product, Side
from rhythm.execution.runner import SwingRunner
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.types import Interval
from rhythm.orders.state import OrderState
from rhythm.risk import engine
from rhythm.risk.engine import CheckStatus, Intent, OrderProposal, RiskOutcome, evaluate
from rhythm.risk.policy import DEFAULT_POLICY, PolicyValidationError, RiskPolicy
from rhythm.strategies.base import (
    EventView,
    LookAheadError,
    PositionView,
    SignalType,
    StrategyContext,
)
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from rhythm.strategies.swing_trend import SwingTrendPullback
from tests.phase3_helpers import KEY, breakout_day, daily, sim_env, uptrend_with_pullback
from tests.unit.test_risk_engine import prop, state

CAL = SessionCalendar()
STRAT = SwingTrendPullback()


def close_of(d: date) -> datetime:
    return CAL.session_close(d)


def ctx_for(bars, **kw) -> StrategyContext:  # type: ignore[no-untyped-def]
    return StrategyContext(
        KEY,
        close_of(bars[-1].start.astimezone(EXCHANGE_TZ).date()),
        tuple(bars),
        bar_end=lambda b: close_of(b.start.astimezone(EXCHANGE_TZ).date()),
        **kw,
    )


def ev(days_ahead: int, bars, known_days_before: int = 30) -> EventView:  # type: ignore[no-untyped-def]
    d = bars[-1].start.astimezone(EXCHANGE_TZ).date() + timedelta(days=days_ahead)
    return EventView("RESULTS", d, close_of(d) - timedelta(days=known_days_before))


def swing_env(*events: CorporateEvent, synced: bool = True):  # type: ignore[no-untyped-def]
    env = sim_env()
    env.ledger_allocation = Decimal(300_000)
    env.policy = replace(DEFAULT_POLICY, version="t", max_single_name_pct=Decimal("0.3"))
    if synced:
        env.events[KEY] = tuple(events)
    return env


# ---------------------------------------------------------------------------- strategy
def test_entry_with_thesis() -> None:
    e = STRAT.evaluate(ctx_for(uptrend_with_pullback()))
    (sig,) = e.signals
    assert sig.type is SignalType.ENTER_LONG and sig.horizon is Horizon.SWING
    assert sig.stop is not None and sig.target is not None and sig.stop < sig.ref_price
    assert sig.target - sig.ref_price == 2 * (sig.ref_price - sig.stop)
    assert sig.thesis and len(sig.thesis["rationale"]) == 2
    rules = {r["rule"] for r in sig.thesis["invalidation"]}
    assert rules == {"close_below_sma", "stop", "max_holding_sessions", "exit_before_event_days"}


def test_not_enough_history_and_no_trend() -> None:
    bars = uptrend_with_pullback()
    assert STRAT.evaluate(ctx_for(bars[-40:])).signals == []
    falling = daily([f"{150 - i * 0.5:.2f}" for i in range(61)])
    e = STRAT.evaluate(ctx_for(falling))
    assert e.signals == [] and e.diagnostics["checks"]["trend"] is False


def test_entry_skipped_near_known_event() -> None:
    bars = uptrend_with_pullback()
    e = STRAT.evaluate(ctx_for(bars, events=(ev(4, bars),)))
    assert e.signals == [] and e.diagnostics["checks"]["no_event_soon"] is False
    assert STRAT.evaluate(ctx_for(bars, events=(ev(9, bars),))).signals


def test_future_announcement_is_look_ahead() -> None:
    bars = uptrend_with_pullback()
    with pytest.raises(LookAheadError):
        ctx_for(bars, events=(ev(10, bars, known_days_before=-1),))


def test_daily_bar_end_is_session_close_not_next_day() -> None:
    bars = uptrend_with_pullback()
    with pytest.raises(LookAheadError):  # default end = start + 24 h > close
        StrategyContext(KEY, close_of(bars[-1].start.date()), tuple(bars))


def _held_ctx(bars, opened_days_ago: int, **kw) -> StrategyContext:  # type: ignore[no-untyped-def]
    opened = bars[-1 - opened_days_ago].start
    pos = PositionView(KEY, 100, Decimal(120), opened)
    return ctx_for(bars, position=pos, **kw)


@pytest.mark.parametrize(
    ("closes_tail", "opened_ago", "event_days", "reason"),
    [
        (["110.00"], 3, None, "thesis invalidated"),  # close below SMA50 (~117)
        (["130.00"], 25, None, "time stop"),
        (["130.00"], 3, 1, "event risk"),
    ],
)
def test_exit_reasons(
    closes_tail: list[str], opened_ago: int, event_days: int | None, reason: str
) -> None:
    closes = [f"{100 + i * 0.5:.2f}" for i in range(60)] + closes_tail
    bars = daily(closes)
    events = () if event_days is None else (ev(event_days, bars),)
    (sig,) = STRAT.evaluate(_held_ctx(bars, opened_ago, events=events)).signals
    assert sig.type is SignalType.EXIT_LONG and any(reason in r for r in sig.reasons)


def test_holding_without_reasons_emits_nothing() -> None:
    bars = daily([f"{100 + i * 0.5:.2f}" for i in range(61)])
    e = STRAT.evaluate(_held_ctx(bars, 3))
    assert e.signals == [] and e.diagnostics["exit_reasons"] == []


# ---------------------------------------------------------------------------- events
def test_mock_events_deterministic_and_announced_ahead() -> None:
    p = MockEventProvider()
    a = p.events(KEY, date(2026, 1, 1), date(2026, 12, 31))
    assert a == MockEventProvider().events(KEY, date(2026, 1, 1), date(2026, 12, 31))
    assert len(a) == 4 and all(e.kind is EventKind.RESULTS and e.is_mock for e in a)
    for e in a:
        assert e.event_date.weekday() < 5
        assert (e.event_date - e.known_at.astimezone(EXCHANGE_TZ).date()).days == ANNOUNCE_DAYS


def test_known_upcoming_is_point_in_time() -> None:
    e = CorporateEvent(
        KEY, EventKind.DIVIDEND, date(2026, 11, 20), close_of(date(2026, 11, 2)), "MANUAL"
    )
    before = close_of(date(2026, 11, 1))
    after = close_of(date(2026, 11, 3))
    assert known_upcoming([e], KEY, before, date(2026, 11, 1)) == []
    assert known_upcoming([e], KEY, after, date(2026, 11, 3)) == [e]
    assert not e.is_mock


# ---------------------------------------------------------------------------- R18
def _swing_prop(**kw: object) -> OrderProposal:
    return prop(horizon=Horizon.SWING, **kw)


@pytest.mark.parametrize(
    ("kw", "ok", "text"),
    [
        ({"event_calendar_synced_at": None}, False, "never synced"),
        ({"days_to_next_event": 3}, False, "blackout"),
        ({"days_to_next_event": 4}, True, "in 4 days"),
        ({"days_to_next_event": None}, True, "none known"),
    ],
)
def test_r18_swing_event_gate(kw: dict[str, object], ok: bool, text: str) -> None:
    base: dict[str, object] = {"event_calendar_synced_at": prop().at - timedelta(days=1)}
    d = evaluate(_swing_prop(), state(**(base | kw)))
    c = next(x for x in d.checks if x.code == "R18")
    assert (c.status is CheckStatus.PASS) is ok and text in c.detail


def test_r18_stale_calendar_and_long_term() -> None:
    old = prop().at - timedelta(days=8)
    d = evaluate(_swing_prop(), state(event_calendar_synced_at=old))
    assert d.outcome is RiskOutcome.BLOCK and "synced 8.0 days ago" in d.failed[0].detail
    lt = evaluate(prop(horizon=Horizon.LONG_TERM), state())
    assert "R18" in {c.code for c in lt.failed}


def test_r3_uses_evaluation_time_for_swing() -> None:
    at = prop().at
    s = state(
        as_of=at - timedelta(hours=18),  # yesterday's close
        reconciled_at=at,
        evaluated_at=at,
        event_calendar_synced_at=at,
    )
    d = evaluate(_swing_prop(at=at - timedelta(hours=18)), s)
    assert next(c for c in d.checks if c.code == "R3").status is CheckStatus.PASS


def test_swing_costs_use_delivery_rates() -> None:
    cm = CostModel()
    intraday = engine._cost_per_share(prop(), cm)
    swing = engine._cost_per_share(_swing_prop(), cm)
    assert swing > intraday  # delivery STT/stamp/brokerage are higher


def test_policy_event_bounds() -> None:
    with pytest.raises(PolicyValidationError):
        RiskPolicy.from_params("x", DEFAULT_POLICY.to_params() | {"event_blackout_days": 99})
    assert DEFAULT_POLICY.version == "paper-default-2"


# ---------------------------------------------------------------------------- runner
def _later(bars, closes: list[str]):  # type: ignore[no-untyped-def]
    """Extend a daily series with more sessions after its last bar."""
    last = bars[-1].start.astimezone(EXCHANGE_TZ).date()
    end = last
    n = 0
    while n < len(closes):
        end += timedelta(days=1)
        if end.weekday() < 5:
            n += 1
    full = daily([str(b.close) for b in bars] + closes, end)
    return full[len(bars) :]


def test_swing_runner_multi_day_lifecycle() -> None:
    bars = uptrend_with_pullback()
    book = PaperBook(cash=Decimal(1_000_000), horizon=Horizon.SWING)
    runner = SwingRunner(STRAT, book, CAL, env=swing_env())
    recs = [r for b in bars for r in runner.on_bar(b)[1]]
    entry = recs[-1]
    assert entry.signal.type is SignalType.ENTER_LONG and entry.order_id is not None
    order = book.orders[entry.order_id]
    assert order.product is Product.DELIVERY and order.state is OrderState.BROKER_ACKNOWLEDGED
    assert order.created_at == close_of(bars[-1].start.date())

    nxt = _later(bars, ["131.00", "132.00", "133.00"])
    runner.on_bar(nxt[0])  # fills at the next session's open
    assert order.state is OrderState.FILLED and book.position(KEY).quantity == order.quantity
    prot = {o.purpose: o for o in book.working_orders(KEY)}
    assert set(prot) == {"stop", "target"} and all(
        o.product is Product.DELIVERY for o in prot.values()
    )
    runner.on_bar(nxt[1])
    runner.on_bar(nxt[2])
    # protection persists across sessions (no end-of-day expiry for swing)
    assert {o.purpose for o in book.working_orders(KEY)} == {"stop", "target"}
    assert all(o.state is OrderState.BROKER_ACKNOWLEDGED for o in prot.values())


def test_swing_entry_blocked_without_event_coverage() -> None:
    runner = SwingRunner(
        STRAT,
        PaperBook(cash=Decimal(1_000_000), horizon=Horizon.SWING),
        CAL,
        env=swing_env(synced=False),
    )
    recs = [r for b in uptrend_with_pullback() for r in runner.on_bar(b)[1]]
    assert recs[-1].order_id is None and [c.code for c in recs[-1].risk.failed] == ["R18"]


def test_unfilled_entry_expires_after_next_session() -> None:
    bars = uptrend_with_pullback()
    book = PaperBook(cash=Decimal(1_000_000), horizon=Horizon.SWING)
    runner = SwingRunner(STRAT, book, CAL, env=swing_env())
    for b in bars:
        runner.on_bar(b)
    (entry,) = [o for o in book.orders.values() if o.purpose == "entry"]
    thin = replace(_later(bars, ["131.00"])[0], volume=0)  # no liquidity -> no fill
    runner.on_bar(thin)
    assert entry.state is OrderState.EXPIRED and "next session" in (entry.reason or "")


def test_swing_runner_rejects_intraday_bars() -> None:
    runner = SwingRunner(STRAT, PaperBook(cash=Decimal(1)), CAL, env=swing_env())
    with pytest.raises(ValueError, match="daily"):
        runner.on_bar(breakout_day()[0])


def test_ledger_pnl_ignores_other_ledgers_cash() -> None:
    """R8 must not see another ledger's purchases as this strategy's loss."""
    book = PaperBook(cash=Decimal(1_000_000), horizon=Horizon.SWING)
    runner = SwingRunner(STRAT, book, CAL, env=swing_env())
    day = date(2026, 10, 30)
    runner.start_day(day)
    book.cash -= Decimal(500_000)  # e.g. the intraday ledger bought something
    assert runner.ledger_day_pnl(day) == 0


def test_prices_snapped_to_tick() -> None:
    bars = uptrend_with_pullback()
    book = PaperBook(cash=Decimal(1_000_000), horizon=Horizon.SWING)
    runner = SwingRunner(STRAT, book, CAL, env=swing_env())
    recs = [r for b in bars for r in runner.on_bar(b)[1]]
    s = recs[-1].signal
    assert s.stop is not None and s.target is not None
    assert s.stop % Decimal("0.05") == 0 and s.target % Decimal("0.05") == 0


# ---------------------------------------------------------------------------- backtests
def _mock_swing_inputs(days: int = 420):  # type: ignore[no-untyped-def]
    end = datetime(2026, 10, 30, 16, 0, tzinfo=EXCHANGE_TZ)
    start = end - timedelta(days=days)
    prov = MockMarketDataProvider(clock=FixedClock(end), calendar=CAL)
    keys = (InstrumentKey(Exchange.NSE, "MOCKA"), InstrumentKey(Exchange.NSE, "MOCKB"))
    bars = {k: list(prov.bars(k, Interval.D1, start, end)) for k in keys}
    events = {
        k: tuple(MockEventProvider().events(k, start.date(), end.date() + timedelta(days=60)))
        for k in keys
    }
    cfg = BacktestConfig(symbols=keys, start=start, end=end, interval=Interval.D1, events=events)
    return cfg, bars


def test_swing_backtest_deterministic_and_event_sensitive() -> None:
    cfg, bars = _mock_swing_inputs()
    a = run_backtest(STRAT, cfg, bars, calendar=CAL)
    b = run_backtest(STRAT, cfg, bars, calendar=CAL)
    assert a.run_hash == b.run_hash and a.metrics == b.metrics and a.metrics["trades"] > 0
    assert a.data_is_mock
    no_events = replace(cfg, events={})
    c = run_backtest(STRAT, no_events, bars, calendar=CAL)
    assert c.run_hash != a.run_hash and c.metrics["trades"] == 0  # R18 blocks: no coverage


def test_swing_backtest_prefix_invariance() -> None:
    cfg, bars = _mock_swing_inputs()
    full = run_backtest(STRAT, cfg, bars, calendar=CAL)
    cut = cfg.end - timedelta(days=90)
    short = run_backtest(STRAT, replace(cfg, end=cut), bars, calendar=CAL)
    closed_before = [t for t in full.trades if t.exit_at < cut - timedelta(days=5)]
    assert closed_before and closed_before == [
        t for t in short.trades if t.exit_at < cut - timedelta(days=5)
    ]


def test_backtest_interval_must_match_horizon() -> None:
    cfg, bars = _mock_swing_inputs(120)
    with pytest.raises(BacktestInputError, match="needs 1d"):
        run_backtest(STRAT, replace(cfg, interval=Interval.M5), bars, calendar=CAL)
    with pytest.raises(BacktestInputError, match="needs 5m"):
        run_backtest(IntradayOrbVwap(), cfg, bars, calendar=CAL)


def test_reduce_intent_for_swing_exit_allowed_near_event() -> None:
    p = _swing_prop(side=Side.SELL, intent=Intent.REDUCE, stop=None, purpose="exit")
    d = evaluate(p, state(ledger_position_qty=10, days_to_next_event=0))
    assert d.accepted and d.quantity == 10


def test_mock_origin_label() -> None:
    assert uptrend_with_pullback()[0].source is DataOrigin.MOCK
