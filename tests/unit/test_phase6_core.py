"""Phase 6 core: fundamentals evidence, long-term strategy, allocation band, R21, runner."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from rhythm.backtest.engine import BacktestConfig, run_backtest
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import Exchange, Horizon, InstrumentKey
from rhythm.events.mock import MockEventProvider
from rhythm.execution.book import PaperBook
from rhythm.execution.costs import Product, Side
from rhythm.execution.runner import DailyRunner
from rhythm.fundamentals.mock import PUBLISH_LAG_DAYS, MockFundamentalsProvider
from rhythm.fundamentals.store import HISTORY_START
from rhythm.fundamentals.types import FundamentalSnapshot, derive, known_as_of
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.types import Interval
from rhythm.risk.engine import CheckStatus, Intent, RiskOutcome, evaluate
from rhythm.risk.policy import DEFAULT_POLICY, PolicyValidationError, RiskPolicy
from rhythm.strategies.base import LookAheadError, PositionView, SignalType, StrategyContext
from rhythm.strategies.long_term import LongTermQualityValue
from tests.phase3_helpers import KEY, daily, sim_env
from tests.unit.test_risk_engine import prop, state

CAL = SessionCalendar()
STRAT = LongTermQualityValue()
CAPITAL = Decimal(400_000)


def close_of(d: date) -> datetime:
    return CAL.session_close(d)


def quarters(
    n: int = 8,
    *,
    eps: str = "5",
    growth: str = "0.02",
    roe: str = "0.20",
    de: str = "0.5",
    ocf_ratio: str = "1.1",
    last_period: date = date(2026, 6, 30),
    lag_days: int = 45,
) -> list[FundamentalSnapshot]:
    """Synthetic quarterly snapshots ending at ``last_period`` (oldest first)."""
    out: list[FundamentalSnapshot] = []
    shares = Decimal(1_000_000)
    for i in range(n):
        back = n - 1 - i
        y, m = last_period.year, last_period.month - 3 * back
        while m <= 0:
            m += 12
            y -= 1
        pe_ = date(y, m, 30 if m in (6, 9) else 31)
        e = (Decimal(eps) / (1 + Decimal(growth)) ** back).quantize(Decimal("0.01"))
        ni = e * shares
        equity = (ni * 4 / Decimal(roe)).quantize(Decimal(1))
        out.append(
            FundamentalSnapshot(
                key=KEY,
                period_end=pe_,
                known_at=close_of(pe_ + timedelta(days=lag_days)),
                source="MOCK",
                source_ref=f"test:{pe_}",
                metrics={
                    "revenue": ni * 8,
                    "net_income": ni,
                    "eps": e,
                    "book_value_per_share": (equity / shares).quantize(Decimal("0.01")),
                    "operating_cash_flow": ni * Decimal(ocf_ratio),
                    "total_debt": equity * Decimal(de),
                    "total_equity": equity,
                },
            )
        )
    return out


def bars_ending(day: date, close: str = "100", n: int = 30):  # type: ignore[no-untyped-def]
    return daily([close] * n, end=day)


def ctx(bars, funds, *, position=None, capital=CAPITAL, **kw) -> StrategyContext:  # type: ignore[no-untyped-def]
    end = bars[-1].start.astimezone(EXCHANGE_TZ).date()
    return StrategyContext(
        KEY,
        close_of(end),
        tuple(bars),
        position,
        fundamentals=tuple(known_as_of(funds, close_of(end))),
        capital=capital,
        bar_end=lambda b: close_of(b.start.astimezone(EXCHANGE_TZ).date()),
        **kw,
    )


DAY = date(2026, 10, 30)


# ---------------------------------------------------------------------------- evidence
def test_derived_metrics_with_formulas() -> None:
    q = quarters()
    d = derive(q, Decimal(100))
    eps_ttm = sum((s.metrics["eps"] for s in q[-4:]), Decimal(0))
    assert d["eps_ttm"].value == eps_ttm and d["pe"].value == Decimal(100) / eps_ttm
    roe = d["roe"].value
    assert roe is not None and Decimal("0.19") < roe <= Decimal("0.20")  # TTM vs latest equity
    assert d["debt_to_equity"].value == Decimal("0.5")
    assert d["eps_growth_yoy"].value is not None and d["eps_growth_yoy"].value > 0
    assert d["pe"].formula == "price / eps_ttm" and "price:last_close" in d["pe"].inputs
    assert "eps_growth_yoy" not in derive(q[-6:], Decimal(100))  # needs 8 quarters
    assert derive(q[-3:], Decimal(100)) == {}


def test_snapshot_requires_all_reported_metrics() -> None:
    with pytest.raises(ValueError, match="missing"):
        FundamentalSnapshot(KEY, date(2026, 6, 30), close_of(DAY), "MOCK", {"eps": Decimal(1)})


def test_known_as_of_is_point_in_time_and_takes_restatements() -> None:
    q = quarters()
    cutoff = q[-1].known_at - timedelta(seconds=1)
    assert known_as_of(q, cutoff) == q[:-1]
    restated = replace(q[-2], known_at=q[-1].known_at + timedelta(days=1))
    later = known_as_of([*q, restated], restated.known_at)
    assert later[-2] is restated and len(later) == len(q)


def test_mock_fundamentals_stable_and_published_with_lag() -> None:
    m = MockMarketDataProvider()
    p = MockFundamentalsProvider(m.price_at)
    k = InstrumentKey(Exchange.NSE, "MOCKE")
    a = p.snapshots(k, date(2024, 1, 1), date(2026, 11, 3))
    b = p.snapshots(k, date(2025, 1, 1), date(2026, 6, 30))
    assert all(x in a for x in b) and all(s.is_mock for s in a)
    for s in a:
        assert (s.known_at.astimezone(EXCHANGE_TZ).date() - s.period_end).days == PUBLISH_LAG_DAYS


def test_fundamentals_look_ahead_guard() -> None:
    q = quarters()
    bars = bars_ending(DAY)
    with pytest.raises(LookAheadError, match="not published"):
        StrategyContext(
            KEY,
            close_of(DAY),
            tuple(bars),
            fundamentals=(replace(q[-1], known_at=close_of(DAY) + timedelta(days=1)),),
            bar_end=lambda b: close_of(b.start.astimezone(EXCHANGE_TZ).date()),
        )


# ---------------------------------------------------------------------------- strategy
def test_entry_with_evidence_thesis_and_target_weight() -> None:
    e = STRAT.evaluate(ctx(bars_ending(DAY), quarters()))  # P/E = 100 / ~19.4 ~ 5
    (sig,) = e.signals
    assert sig.type is SignalType.ENTER_LONG and sig.horizon is Horizon.LONG_TERM
    assert sig.target_weight == Decimal("0.10") and sig.stop is None
    th = sig.thesis
    assert th and {r["rule"] for r in th["invalidation"]} == {
        "roe_below",
        "ocf_ttm_not_positive",
        "eps_growth_below",
    }
    assert th["key_metrics"]["pe"]["formula"] == "price / eps_ttm"
    assert th["evidence"][0]["source"] == "MOCK" and th["evidence"][0]["period_end"] == "2026-06-30"
    assert th["allocation_band"] == {"min": "0.05", "target": "0.10", "max": "0.15"}


@pytest.mark.parametrize(
    ("kw", "check"),
    [
        ({"roe": "0.12"}, "quality"),
        ({"de": "1.5"}, "quality"),
        ({"ocf_ratio": "-0.1"}, "quality"),
        ({"growth": "-0.01"}, "quality"),
        ({"eps": "0.5"}, "valuation_ok"),  # P/E ~ 50
        ({"last_period": date(2026, 3, 31), "lag_days": 15}, "fundamentals_fresh"),
    ],
)
def test_entry_blocked(kw: dict[str, object], check: str) -> None:
    if check == "fundamentals_fresh":
        q = quarters(last_period=date(2025, 12, 31), lag_days=10)
    else:
        q = quarters(**kw)  # type: ignore[arg-type]
    e = STRAT.evaluate(ctx(bars_ending(DAY), q))
    assert e.signals == [] and e.diagnostics["entry_checks"][check] is False


def test_insufficient_history_and_unknown_capital() -> None:
    e = STRAT.evaluate(ctx(bars_ending(DAY), quarters(5)))
    assert e.signals == [] and "need 8" in e.diagnostics["reason"]
    e2 = STRAT.evaluate(ctx(bars_ending(DAY), quarters(), capital=None))
    assert e2.signals == [] and "capital unknown" in e2.diagnostics["reason"]


def _held(bars, qty: int, sessions_ago: int) -> PositionView:  # type: ignore[no-untyped-def]
    return PositionView(KEY, qty, Decimal(100), bars[-1 - sessions_ago].start)


def test_thesis_invalidated_exits_everything() -> None:
    bars = bars_ending(DAY)
    e = STRAT.evaluate(ctx(bars, quarters(roe="0.08"), position=_held(bars, 400, 3)))
    (sig,) = e.signals
    assert sig.type is SignalType.EXIT_LONG and "ROE below" in sig.reasons[0]


def test_holds_between_reviews() -> None:
    bars = bars_ending(DAY)
    e = STRAT.evaluate(ctx(bars, quarters(), position=_held(bars, 400, 3)))
    assert e.signals == [] and e.diagnostics["reviewed"] is False


@pytest.mark.parametrize(
    ("qty", "eps", "kind", "target"),
    [
        (800, "5", SignalType.REDUCE_LONG, "0.10"),  # weight 20 % > max 15 %
        (100, "5", SignalType.ADD_LONG, "0.10"),  # weight 2.5 % < min 5 %
        (400, "0.3", SignalType.REDUCE_LONG, "0.05"),  # P/E ~ 80 > 45: trim to min
    ],
)
def test_review_rebalances_within_band(qty: int, eps: str, kind: SignalType, target: str) -> None:
    bars = bars_ending(DAY)
    q = quarters(eps=eps, growth="0.0")
    e = STRAT.evaluate(ctx(bars, q, position=_held(bars, qty, 20)))  # review every 20
    (sig,) = e.signals
    assert sig.type is kind and sig.target_weight == Decimal(target)
    assert e.diagnostics["reviewed"] is True


def test_new_results_trigger_a_review() -> None:
    after = daily(["100"] * 40, end=date(2026, 11, 2))
    q = quarters(9, last_period=date(2026, 9, 30), lag_days=33)  # published 2026-11-02 close
    e = STRAT.evaluate(ctx(after, q, position=_held(after, 400, 3)))
    assert e.diagnostics["new_results"] is True and e.diagnostics["reviewed"] is True
    e2 = STRAT.evaluate(ctx(after[:-1], q, position=_held(after[:-1], 400, 3)))
    assert e2.diagnostics["latest_period"] == "2026-06-30"  # not yet published the day before


# ---------------------------------------------------------------------------- engine
def _lt(**kw: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {"horizon": Horizon.LONG_TERM, "stop": None, "quantity": 100}
    return prop(**(base | kw))


def _lt_state(**kw: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "ledger_allocation": CAPITAL,
        "event_calendar_synced_at": prop().at - timedelta(days=1),
        "fundamentals_published_at": prop().at - timedelta(days=40),
    }
    return state(**(base | kw))


def test_long_term_sized_by_allocation_band() -> None:
    d = evaluate(_lt(quantity=1000), _lt_state(ledger_position_qty=200))
    r7 = next(c for c in d.checks if c.code == "R7")
    # band max 15 % of 400k = 60k; holding 200 x 100 = 20k -> room 400 shares
    assert r7.status is CheckStatus.REDUCE and d.quantity == 400 and "band max" in r7.detail
    assert evaluate(_lt(quantity=None), _lt_state()).outcome is RiskOutcome.BLOCK


@pytest.mark.parametrize(
    ("kw", "code", "text"),
    [
        ({"fundamentals_published_at": None}, "R21", "no published"),
        ({"fundamentals_published_at": prop().at - timedelta(days=201)}, "R21", "201 days"),
        ({"event_calendar_synced_at": None}, "R18", "never synced"),
        ({"days_to_next_event": 2}, "R18", "blackout"),
    ],
)
def test_long_term_gates(kw: dict[str, object], code: str, text: str) -> None:
    d = evaluate(_lt(), _lt_state(**kw))
    c = next(x for x in d.checks if x.code == code)
    assert d.outcome is RiskOutcome.BLOCK and c.status is CheckStatus.FAIL and text in c.detail


def test_r21_skipped_for_other_horizons_and_reduce_allowed() -> None:
    d = evaluate(prop(), state())
    assert next(c for c in d.checks if c.code == "R21").status is CheckStatus.SKIP
    sell = _lt(side=Side.SELL, intent=Intent.REDUCE, purpose="reduce", quantity=50)
    d2 = evaluate(sell, _lt_state(fundamentals_published_at=None, ledger_position_qty=80))
    assert d2.accepted and d2.quantity == 50


def test_policy_long_term_bounds() -> None:
    with pytest.raises(PolicyValidationError):
        RiskPolicy.from_params("x", DEFAULT_POLICY.to_params() | {"long_term_max_weight": "0.9"})
    with pytest.raises(PolicyValidationError):
        RiskPolicy.from_params("x", DEFAULT_POLICY.to_params() | {"max_fundamentals_age_days": 5})


# ---------------------------------------------------------------------------- runner
def _env(q: list[FundamentalSnapshot]):  # type: ignore[no-untyped-def]
    env = sim_env()
    env.ledger_allocation = CAPITAL
    env.events[KEY] = ()
    env.fundamentals[KEY] = tuple(q)
    return env


def test_runner_entry_quantity_from_target_weight_and_no_stop_orders() -> None:
    q = quarters()
    bars = bars_ending(DAY)
    book = PaperBook(cash=Decimal(1_000_000), horizon=Horizon.LONG_TERM)
    runner = DailyRunner(STRAT, book, CAL, env=_env(q))
    recs = [r for b in bars for r in runner.on_bar(b)[1]]
    entry = next(r for r in recs if r.signal.type is SignalType.ENTER_LONG)
    order = book.orders[entry.order_id]  # type: ignore[index]
    assert order.quantity == 400 and order.product is Product.DELIVERY  # 10 % of 400k / 100
    nxt = daily(["100", "100"], end=DAY + timedelta(days=4))  # following Mon/Tue
    for b in nxt:
        runner.on_bar(b)
    assert book.position(KEY).quantity == 400
    assert [o for o in book.working_orders(KEY) if o.purpose in ("stop", "target")] == []


def test_runner_rebalance_orders() -> None:
    q = quarters(growth="0.0")
    bars = bars_ending(DAY, n=40)
    book = PaperBook(cash=Decimal(1_000_000), horizon=Horizon.LONG_TERM)
    runner = DailyRunner(STRAT, book, CAL, env=_env(q))
    pos = book.position(KEY)
    pos.quantity, pos.average_price, pos.opened_at = 800, Decimal(100), bars[-21].start
    runner.history[KEY] = list(bars[:-1])
    recs = runner.on_bar(bars[-1])[1]
    (rec,) = recs
    assert rec.signal.type is SignalType.REDUCE_LONG
    order = book.orders[rec.order_id]  # type: ignore[index]
    assert order.side is Side.SELL and order.purpose == "reduce" and order.quantity == 400


def test_long_term_backtest_deterministic_and_fundamentals_sensitive() -> None:
    end = datetime(2026, 10, 30, 16, 0, tzinfo=EXCHANGE_TZ)
    start = end - timedelta(days=500)
    m = MockMarketDataProvider(clock=FixedClock(end), calendar=CAL)
    fp = MockFundamentalsProvider(m.price_at)
    keys = (InstrumentKey(Exchange.NSE, "MOCKE"), InstrumentKey(Exchange.NSE, "MOCKA"))
    bars = {k: list(m.bars(k, Interval.D1, start, end)) for k in keys}
    ev = {k: tuple(MockEventProvider().events(k, start.date(), end.date())) for k in keys}
    fund = {k: tuple(fp.snapshots(k, HISTORY_START, end.date())) for k in keys}
    cfg = BacktestConfig(
        symbols=keys, start=start, end=end, interval=Interval.D1, events=ev, fundamentals=fund
    )
    a = run_backtest(STRAT, cfg, bars, calendar=CAL)
    b = run_backtest(STRAT, cfg, bars, calendar=CAL)
    assert a.run_hash == b.run_hash and a.metrics == b.metrics
    entries = [s for s in a.signals if s.order_id and s.signal.type is SignalType.ENTER_LONG]
    assert {s.signal.key.symbol for s in entries} == {"MOCKE"}  # only the quality name
    assert a.metrics["positions_open_at_end"] >= 1
    none = run_backtest(STRAT, replace(cfg, fundamentals={}), bars, calendar=CAL)
    assert none.run_hash != a.run_hash and not [s for s in none.signals if s.order_id]
