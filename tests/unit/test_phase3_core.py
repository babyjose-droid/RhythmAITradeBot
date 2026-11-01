from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from rhythm.core.market import DataOrigin
from rhythm.execution.book import BookError, Order, PaperBook
from rhythm.execution.costs import CostModel, Product, Side
from rhythm.execution.fills import FillModel, OrderType
from rhythm.orders.state import (
    DisplayCategory,
    IllegalTransitionError,
    OrderState,
    check_transition,
    display_category,
)
from rhythm.strategies.base import (
    LookAheadError,
    PositionView,
    SignalType,
    StrategyContext,
    params_hash,
)
from rhythm.strategies.intraday_orb import IntradayOrbVwap, OrbParams
from tests.phase3_helpers import KEY, bar, breakout_day, opening_range, t

S = OrderState


# ------------------------------------------------------------------ order state machine
def test_happy_path_and_illegal_transitions() -> None:
    path = [
        S.PROPOSED,
        S.RISK_CHECKED,
        S.SUBMITTED,
        S.BROKER_ACKNOWLEDGED,
        S.PARTIALLY_FILLED,
        S.FILLED,
        S.RECONCILED,
    ]
    for a, b in itertools.pairwise(path):
        check_transition(a, b)
    for bad in [
        (S.PROPOSED, S.SUBMITTED),
        (S.PROPOSED, S.FILLED),
        (S.FILLED, S.CANCELLED),
        (S.RECONCILED, S.FILLED),
    ]:
        with pytest.raises(IllegalTransitionError):
            check_transition(*bad)


def test_four_ui_categories_are_distinct() -> None:
    cats = {
        display_category(S.RISK_CHECKED, 0),
        display_category(S.BROKER_ACKNOWLEDGED, 0),
        display_category(S.PARTIALLY_FILLED, 3),
        display_category(S.FILLED, 10),
    }
    assert cats == {
        DisplayCategory.APPROVED_NOT_SENT,
        DisplayCategory.AT_BROKER_NOT_FILLED,
        DisplayCategory.PARTIALLY_FILLED,
        DisplayCategory.FILLED,
    }
    assert display_category(S.CANCELLED, 0) is DisplayCategory.CLOSED_WITHOUT_FILL
    assert display_category(S.UNKNOWN, 0) is DisplayCategory.NEEDS_ATTENTION


# ------------------------------------------------------------------ costs
def test_intraday_charges_by_side() -> None:
    cm = CostModel()
    buy = cm.charges(Side.BUY, 100, Decimal("1000"), Product.INTRADAY)
    sell = cm.charges(Side.SELL, 100, Decimal("1000"), Product.INTRADAY)
    assert buy.stt == 0 and sell.stt == Decimal("25.00")  # 0.025 % on sell value 1,00,000
    assert buy.stamp == Decimal("3.00") and sell.stamp == 0
    assert buy.gst == ((buy.brokerage + buy.exchange_txn + buy.sebi) * Decimal("0.18")).quantize(
        Decimal("0.01")
    )
    assert cm.charges(Side.BUY, 0, Decimal(1), Product.INTRADAY).total == 0
    assert cm.describe()["verified"] is False


# ------------------------------------------------------------------ fills
FM = FillModel(slippage_bps=Decimal(10), max_participation=Decimal("0.5"))
TICK = Decimal("0.05")
B = bar(t(10, 0), "100", "101", "99", "100.5", v=100)


def test_market_fills_at_open_with_adverse_slippage() -> None:
    buy = FM.simulate(side=Side.BUY, order_type=OrderType.MARKET, remaining=10, bar=B, tick=TICK)
    sell = FM.simulate(side=Side.SELL, order_type=OrderType.MARKET, remaining=10, bar=B, tick=TICK)
    assert buy is not None and buy.price == Decimal("100.10")
    assert sell is not None and sell.price == Decimal("99.90")


def test_participation_cap_gives_partial_fills() -> None:
    q = FM.simulate(side=Side.BUY, order_type=OrderType.MARKET, remaining=500, bar=B, tick=TICK)
    assert q is not None and q.quantity == 50


def test_limit_and_stop_rules() -> None:
    assert (
        FM.simulate(
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            remaining=1,
            bar=B,
            tick=TICK,
            limit_price=Decimal("102"),
        )
        is None
    )
    lim = FM.simulate(
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        remaining=1,
        bar=B,
        tick=TICK,
        limit_price=Decimal("100.8"),
    )
    assert lim is not None and lim.price == Decimal("100.8")
    gap = bar(t(10, 5), "97", "98", "96", "97.5")
    stop = FM.simulate(
        side=Side.SELL,
        order_type=OrderType.STOP,
        remaining=1,
        bar=gap,
        tick=TICK,
        stop_price=Decimal("99"),
    )
    assert stop is not None and stop.price < Decimal("97")  # gap: open minus slippage, not the stop


# ------------------------------------------------------------------ paper book
def _book() -> PaperBook:
    return PaperBook(cash=Decimal(100_000), fill_model=FM)


def _working(
    book: PaperBook, side: Side, qty: int, otype: OrderType = OrderType.MARKET, **kw: Any
) -> Order:
    o, _created = book.propose(
        key=KEY,
        side=side,
        order_type=otype,
        quantity=qty,
        at=t(9, 55),
        idempotency_key=f"k{len(book.orders)}",
        **kw,
    )
    book.transition(o, S.RISK_CHECKED, t(9, 55))
    book.submit(o, t(9, 55))
    return o


def test_idempotency_and_accounting() -> None:
    book = _book()
    o, created = book.propose(
        key=KEY,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=10,
        at=t(9, 55),
        idempotency_key="same",
    )
    dup, created2 = book.propose(
        key=KEY,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=99,
        at=t(9, 56),
        idempotency_key="same",
    )
    assert created and not created2 and dup is o and len(book.orders) == 1
    book.transition(o, S.RISK_CHECKED, t(9, 55))
    book.submit(o, t(9, 55))
    (f,) = book.on_bar(B)
    pos = book.position(KEY)
    assert pos.quantity == 10 and pos.average_price == f.price
    assert book.cash == Decimal(100_000) - f.price * 10 - f.charges.total
    _working(book, Side.SELL, 10)
    (f2,) = book.on_bar(bar(t(10, 5), "102", "103", "101", "102"))
    assert pos.quantity == 0 and pos.realized_pnl == (f2.price - f.price) * 10
    assert [e.to_state for e in book.events if e.order_id == o.id] == [
        S.PROPOSED,
        S.RISK_CHECKED,
        S.SUBMITTED,
        S.BROKER_ACKNOWLEDGED,
        S.FILLED,
    ]


def test_short_selling_refused() -> None:
    book = _book()
    o = _working(book, Side.BUY, 1)
    with pytest.raises(BookError, match="short"):
        book.apply_fill(replace(o, side=Side.SELL), 5, Decimal(100), t(10, 0))


def test_oco_stop_wins_when_bar_hits_both() -> None:
    book = _book()
    _working(book, Side.BUY, 10)
    book.on_bar(B)
    stop = _working(
        book,
        Side.SELL,
        10,
        OrderType.STOP,
        stop_price=Decimal("99.5"),
        purpose="stop",
        oco_group="g",
    )
    tgt = _working(
        book,
        Side.SELL,
        10,
        OrderType.LIMIT,
        limit_price=Decimal("101"),
        purpose="target",
        oco_group="g",
    )
    book.on_bar(bar(t(10, 5), "100", "101.5", "99", "100"))  # touches both
    assert stop.state is S.FILLED and tgt.state is S.CANCELLED


def test_partial_then_full_and_expiry() -> None:
    book = _book()
    o = _working(book, Side.BUY, 80)
    book.on_bar(B)  # cap 50
    assert o.state is S.PARTIALLY_FILLED and o.filled_quantity == 50
    book.on_bar(bar(t(10, 5), "100", "101", "99", "100", v=100))
    assert o.state is S.FILLED
    p = _working(book, Side.BUY, 10, OrderType.LIMIT, limit_price=Decimal("50"))
    assert book.expire_working(t(15, 30)) == 1 and p.state is S.EXPIRED


# ------------------------------------------------------------------ strategy
STRAT = IntradayOrbVwap()


def ctx(bars, now, position=None, entries=0):  # type: ignore[no-untyped-def]
    return StrategyContext(KEY, now, tuple(bars), position, entries)


def test_breakout_produces_entry_with_stop_and_target() -> None:
    ev = STRAT.evaluate(ctx(breakout_day(), t(9, 35)))
    (sig,) = ev.signals
    assert sig.type is SignalType.ENTER_LONG and sig.ref_price == Decimal("101.8")
    assert sig.stop == Decimal("100") and sig.target == Decimal("105.4")
    assert sig.at == t(9, 35)


def test_no_entry_when_conditions_fail() -> None:
    assert STRAT.evaluate(ctx(opening_range(), t(9, 30))).signals == []  # OR only
    weak = [*opening_range(), bar(t(9, 30), "100.5", "102", "100.4", "101.8", 10)]
    assert STRAT.evaluate(ctx(weak, t(9, 35))).diagnostics["checks"]["volume"] is False
    assert STRAT.evaluate(ctx(breakout_day(), t(9, 35), entries=1)).signals == []
    tight = IntradayOrbVwap(OrbParams(max_stop_pct=Decimal("0.01")))
    assert (
        tight.evaluate(ctx(breakout_day(), t(9, 35))).diagnostics["reason"]
        == "stop distance outside limits"
    )


def test_exits_on_vwap_break_and_time() -> None:
    pos = PositionView(KEY, 10, Decimal("101.8"), t(9, 35))
    falling = [*breakout_day(), bar(t(9, 35), "101.8", "101.9", "99", "99.2", 3000)]
    (sig,) = STRAT.evaluate(ctx(falling, t(9, 40), pos)).signals
    assert sig.type is SignalType.EXIT_LONG and sig.reasons == ("vwap_exit",)
    late = [*breakout_day(), bar(t(15, 0), "102", "102.5", "101.9", "102.4", 3000)]
    (sig2,) = STRAT.evaluate(ctx(late, t(15, 5), pos)).signals
    assert sig2.reasons == ("time_exit",)


def test_context_rejects_future_bars() -> None:
    with pytest.raises(LookAheadError):
        ctx(breakout_day(), t(9, 34))  # last bar ends 09:35


def test_params_hash_is_stable_and_sensitive() -> None:
    assert params_hash(STRAT.params) == params_hash(IntradayOrbVwap().params)
    assert params_hash(STRAT.params) != params_hash(
        IntradayOrbVwap(OrbParams(reward_risk=Decimal(3))).params
    )


def test_mock_origin_label_present() -> None:
    assert breakout_day()[0].source is DataOrigin.MOCK and breakout_day()[0].start + timedelta(
        minutes=5
    ) == t(9, 20)
