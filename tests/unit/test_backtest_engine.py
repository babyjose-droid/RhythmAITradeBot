from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from rhythm.backtest.engine import (
    BacktestConfig,
    BacktestInputError,
    Split,
    run_backtest,
    run_splits,
)
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.execution.book import PaperBook
from rhythm.execution.costs import CostModel
from rhythm.execution.fills import FillModel
from rhythm.execution.runner import IntradayRunner
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.types import Interval
from rhythm.orders.state import OrderState
from rhythm.strategies.base import DecisionState
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from tests.phase3_helpers import KEY, bar, breakout_day, sim_env, t

CAL = SessionCalendar()
END = datetime(2026, 10, 31, 0, 0, tzinfo=EXCHANGE_TZ)
KEYS = tuple(InstrumentKey(Exchange.NSE, s) for s in ("MOCKA", "MOCKB", "MOCKC", "MOCKD"))
PROVIDER = MockMarketDataProvider(clock=FixedClock(END), calendar=CAL)
BARS = {k: list(PROVIDER.bars(k, Interval.M5, END - timedelta(days=28), END)) for k in KEYS}


def cfg(start: datetime, end: datetime, **kw: object) -> BacktestConfig:
    return BacktestConfig(symbols=KEYS, start=start, end=end, **kw)  # type: ignore[arg-type]


FULL = cfg(END - timedelta(days=28), END)


# ------------------------------------------------------------------ runner mechanics
def test_runner_entry_fills_next_bar_and_protection_attached() -> None:
    book = PaperBook(cash=Decimal(1_000_000), fill_model=FillModel(max_participation=Decimal(1)))
    runner = IntradayRunner(IntradayOrbVwap(), book, CAL, env=sim_env())
    recs = []
    for b in breakout_day():
        recs += runner.on_bar(b)[1]
    (rec,) = recs
    assert rec.decision is DecisionState.BUY_CANDIDATE and rec.order_id is not None
    entry = book.orders[rec.order_id]
    assert (
        entry.state is OrderState.BROKER_ACKNOWLEDGED and entry.filled_quantity == 0
    )  # not filled yet
    runner.on_bar(bar(t(9, 35), "101.9", "102.5", "101.5", "102.2", 4000))
    assert entry.state is OrderState.FILLED
    assert book.fills[0].at == t(9, 35) and book.fills[0].price >= Decimal("101.9")  # next bar open
    prot = {o.purpose: o for o in book.working_orders(KEY)}
    assert set(prot) == {"stop", "target"} and prot["stop"].stop_price == Decimal("100")
    assert prot["stop"].oco_group == prot["target"].oco_group


def test_same_bar_stop_after_entry_is_conservative() -> None:
    book = PaperBook(cash=Decimal(1_000_000), fill_model=FillModel(max_participation=Decimal(1)))
    runner = IntradayRunner(IntradayOrbVwap(), book, CAL, env=sim_env())
    for b in breakout_day():
        runner.on_bar(b)
    runner.on_bar(
        bar(t(9, 35), "101.9", "102", "99.5", "99.8", 9000)
    )  # entry bar crashes through stop
    assert book.position(KEY).quantity == 0
    assert [book.orders[f.order_id].purpose for f in book.fills] == ["entry", "stop"]


def test_session_square_off_leaves_no_overnight_position() -> None:
    book = PaperBook(cash=Decimal(1_000_000), fill_model=FillModel(max_participation=Decimal(1)))
    runner = IntradayRunner(IntradayOrbVwap(), book, CAL, env=sim_env())
    for b in breakout_day():
        runner.on_bar(b)
    runner.on_bar(bar(t(9, 35), "101.9", "102.5", "101.5", "102.2", 4000))
    runner.on_bar(bar(t(15, 25), "102.2", "102.6", "102.0", "102.4", 4000))  # last bar of session
    assert book.position(KEY).quantity == 0
    assert book.working_orders() == []
    assert book.orders[book.fills[-1].order_id].purpose == "square_off"


# ------------------------------------------------------------------ backtest properties
def test_backtest_is_reproducible() -> None:
    a = run_backtest(IntradayOrbVwap(), FULL, BARS, calendar=CAL)
    b = run_backtest(IntradayOrbVwap(), FULL, BARS, calendar=CAL)
    assert a.run_hash == b.run_hash and a.metrics == b.metrics and a.trades == b.trades
    assert a.data_is_mock is True and a.bars_processed > 0


def test_no_look_ahead_prefix_invariance() -> None:
    """Results up to a cut-off must not change when later data exists (look-ahead canary)."""
    cut = END - timedelta(days=14)
    short = run_backtest(IntradayOrbVwap(), cfg(FULL.start, cut), BARS, calendar=CAL)
    long = run_backtest(IntradayOrbVwap(), FULL, BARS, calendar=CAL)
    early = [tr for tr in long.trades if tr.exit_at < cut]
    assert short.trades == early
    assert len(short.trades) > 0


def test_changing_future_bars_does_not_change_past_trades() -> None:
    cut = END - timedelta(days=14)
    tampered = {
        k: [b if b.start < cut else bar(b.start, "1", "1", "1", "1", 1, key=k) for b in bs]
        for k, bs in BARS.items()
    }
    base = run_backtest(IntradayOrbVwap(), FULL, BARS, calendar=CAL)
    alt = run_backtest(IntradayOrbVwap(), FULL, tampered, calendar=CAL)
    assert [t_ for t_ in base.trades if t_.exit_at < cut] == [
        t_ for t_ in alt.trades if t_.exit_at < cut
    ]
    assert base.run_hash != alt.run_hash  # different data, different provenance


def test_all_trades_are_intraday_and_costs_matter() -> None:
    r = run_backtest(IntradayOrbVwap(), FULL, BARS, calendar=CAL)
    for tr in r.trades:
        assert (
            tr.entry_at.astimezone(EXCHANGE_TZ).date() == tr.exit_at.astimezone(EXCHANGE_TZ).date()
        )
        assert tr.exit_at >= tr.entry_at and tr.charges > 0  # same-bar stop-outs are possible
    free = CostModel(
        brokerage_rate_intraday=Decimal(0),
        stt_intraday_sell=Decimal(0),
        exchange_txn_rate=Decimal(0),
        sebi_per_crore=Decimal(0),
        stamp_intraday_buy=Decimal(0),
    )
    r_free = run_backtest(
        IntradayOrbVwap(), cfg(FULL.start, FULL.end, cost_model=free), BARS, calendar=CAL
    )
    assert Decimal(r_free.metrics["end_equity"]) > Decimal(r.metrics["end_equity"])
    for key in (
        "max_drawdown_pct",
        "win_rate_pct",
        "expectancy_per_trade",
        "turnover_x",
        "exposure_pct",
        "benchmark_equal_weight_buy_hold_pct",
        "total_charges",
    ):
        assert key in r.metrics


def test_splits_are_independent_and_must_not_overlap() -> None:
    mid = END - timedelta(days=14)
    res = run_splits(
        IntradayOrbVwap(),
        FULL,
        BARS,
        [Split("dev", FULL.start, mid), Split("oos", mid, END)],
        calendar=CAL,
    )
    assert set(res) == {"dev", "oos"}
    assert res["dev"].metrics["start_equity"] == res["oos"].metrics["start_equity"]
    with pytest.raises(BacktestInputError, match="overlap"):
        run_splits(
            IntradayOrbVwap(),
            FULL,
            BARS,
            [Split("a", FULL.start, mid + timedelta(days=1)), Split("b", mid, END)],
        )


def test_bad_inputs_rejected() -> None:
    with pytest.raises(BacktestInputError):
        run_backtest(IntradayOrbVwap(), cfg(END, END - timedelta(days=1)), BARS)
    daily = {k: list(PROVIDER.bars(k, Interval.D1, FULL.start, END)) for k in KEYS}
    with pytest.raises(BacktestInputError):
        run_backtest(IntradayOrbVwap(), FULL, daily)
