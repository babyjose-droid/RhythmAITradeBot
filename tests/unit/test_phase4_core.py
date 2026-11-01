"""Phase 4 core: UNKNOWN orders, reconciliation logic, runner + risk engine integration."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.execution.book import PaperBook
from rhythm.execution.costs import Side
from rhythm.execution.fills import OrderType
from rhythm.execution.gateway import FlakyVenue, PaperVenue
from rhythm.execution.runner import IntradayRunner
from rhythm.orders.state import OrderState
from rhythm.portfolio.reconcile import (
    FillFact,
    OrderFact,
    PositionFact,
    consolidate,
    reconcile_paper,
    reconcile_with_venue,
)
from rhythm.risk.engine import RiskOutcome
from rhythm.strategies.base import DecisionState
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from tests.phase3_helpers import KEY, bar, breakout_day, sim_env, t

CAL = SessionCalendar()
OTHER = InstrumentKey(Exchange.NSE, "MOCKB")


def _book(venue: PaperVenue | None = None) -> PaperBook:
    return PaperBook(cash=Decimal(1_000_000), venue=venue or PaperVenue())


def _order(book: PaperBook, idem: str = "a", qty: int = 10, key: InstrumentKey = KEY):  # type: ignore[no-untyped-def]
    o, _ = book.propose(
        key=key,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=qty,
        at=t(10, 0),
        idempotency_key=idem,
    )
    book.transition(o, OrderState.RISK_CHECKED, t(10, 0))
    book.submit(o, t(10, 0))
    return o


# ---------------------------------------------------------------------------- UNKNOWN
def test_paper_venue_acknowledges_with_venue_id() -> None:
    o = _order(_book())
    assert o.state is OrderState.BROKER_ACKNOWLEDGED and o.venue_order_id == "PAPER-00000001"


def test_timeout_after_accept_goes_unknown_then_matches() -> None:
    book = _book(FlakyVenue(mode="timeout_after_accept"))
    o = _order(book)
    assert o.state is OrderState.UNKNOWN and o.venue_order_id is None
    assert book.resolve_unknown(t(10, 1)) == {o.id: "ACKNOWLEDGED"}
    assert o.state is OrderState.BROKER_ACKNOWLEDGED and o.venue_order_id is not None
    # resolved order is not matched twice and nothing was re-sent
    assert len(book.venue.day_orders()) == 1


def test_timeout_before_accept_stays_pending_then_cancelled() -> None:
    book = _book(FlakyVenue(mode="timeout_before_accept"))
    o = _order(book)
    assert book.resolve_unknown(t(10, 1)) == {o.id: "PENDING"}
    assert o.state is OrderState.UNKNOWN
    assert book.resolve_unknown(t(10, 5)) == {o.id: "NOT_FOUND"}
    assert o.state is OrderState.CANCELLED and "not found" in (o.reason or "")


def test_ambiguous_match_stays_unknown() -> None:
    venue = FlakyVenue(mode="timeout_after_accept", fail_next=2)
    book = _book(venue)
    a = _order(book, "a")
    b = _order(book, "b")
    # two identical venue orders, two UNKNOWN orders: each could be either -> do not guess
    venue.orders.append(replace(venue.orders[0], venue_order_id="X-extra"))
    out = book.resolve_unknown(t(10, 1))
    assert out == {a.id: "AMBIGUOUS", b.id: "AMBIGUOUS"}
    assert a.state is b.state is OrderState.UNKNOWN


def test_venue_rejection() -> None:
    o = _order(_book(FlakyVenue(mode="reject")))
    assert o.state is OrderState.BROKER_REJECTED


def test_unknown_order_blocks_new_entries_on_that_instrument() -> None:
    book = _book(FlakyVenue(mode="timeout_after_accept", fail_next=1))
    _order(book, "x")
    runner = IntradayRunner(IntradayOrbVwap(), book, CAL, env=sim_env())
    recs = [r for b in breakout_day() for r in runner.on_bar(b)[1]]
    entry = next(r for r in recs if r.signal.type.value == "ENTER_LONG")
    assert entry.decision is DecisionState.REJECTED_BY_RISK
    assert [c.code for c in entry.risk.failed] == ["R16"]


# ---------------------------------------------------------------------------- runner gating
def test_kill_switch_blocks_entries_but_allows_protection() -> None:
    env = sim_env()
    runner = IntradayRunner(IntradayOrbVwap(), _book(), CAL, env=env)
    recs = []
    after = [
        bar(t(9, 35), "101.9", "102.2", "101.6", "102", 3000),
        bar(t(9, 40), "102", "102.4", "101.8", "102.2", 3000),
    ]
    for b in [*breakout_day(), *after]:
        if b.start >= t(9, 35):
            env.kill_switch_active = True  # entry order already at the venue
        recs += runner.on_bar(b)[1]
    assert runner.book.position(KEY).quantity > 0  # entry filled at 09:35 open
    entries = [r for r in recs if r.signal.type.value == "ENTER_LONG"]
    assert entries and entries[0].order_id is not None
    prot = [r for r in runner.risk_log if r.proposal.purpose in ("stop", "target")]
    assert prot and all(r.decision.accepted for r in prot)


def test_paused_strategy_and_missing_facts_fail_closed() -> None:
    env = sim_env()
    env.paused_strategies = frozenset({IntradayOrbVwap().name})
    runner = IntradayRunner(IntradayOrbVwap(), _book(), CAL, env=env)
    recs = [r for b in breakout_day() for r in runner.on_bar(b)[1]]
    assert all(r.order_id is None for r in recs)
    env2 = sim_env()
    env2.instruments.clear()
    runner2 = IntradayRunner(IntradayOrbVwap(), _book(), CAL, env=env2)
    recs2 = [r for b in breakout_day() for r in runner2.on_bar(b)[1]]
    assert recs2 and all(r.risk.outcome is RiskOutcome.BLOCK for r in recs2)
    assert {"R5", "R6"} <= {c.code for c in recs2[0].risk.failed}


def test_stale_quote_blocks_entry_and_every_decision_is_logged() -> None:
    runner = IntradayRunner(IntradayOrbVwap(), _book(), CAL, env=sim_env())
    recs = [r for b in breakout_day() for r in runner.on_bar(b, quote_status="STALE")[1]]
    assert recs and all(r.order_id is None for r in recs)
    assert len(runner.risk_log) == len(recs)
    assert runner.risk_log[0].state.quote_status == "STALE"


def test_adv_estimated_then_from_prior_sessions() -> None:
    runner = IntradayRunner(IntradayOrbVwap(), _book(), CAL, env=sim_env())
    bars = breakout_day()
    runner.on_bar(bars[0])
    adv, src = runner._adv(KEY, bars[0].start.astimezone(EXCHANGE_TZ).date())
    assert adv is not None and "estimated" in src
    day = bars[0].start.astimezone(EXCHANGE_TZ).date()
    runner.day_volume[(KEY, day - timedelta(days=1))] = 123_000
    adv2, src2 = runner._adv(KEY, day)
    assert adv2 == 123_000 and "prior" in src2


# ---------------------------------------------------------------------------- reconciliation
def _fill(
    side: str, qty: int, price: str = "100", oid: str = "o1", h: str = "INTRADAY"
) -> FillFact:
    return FillFact(h, KEY, side, qty, Decimal(price), Decimal("1"), oid)


def test_reconcile_ok() -> None:
    r = reconcile_paper(
        starting_cash=Decimal(10_000),
        cash=Decimal(10_000) - Decimal(1000) - 1 + Decimal(440) - 1,
        positions=[PositionFact("INTRADAY", KEY, 6)],
        fills=[_fill("BUY", 10), _fill("SELL", 4, "110", "o2")],
        orders=[OrderFact("o1", 10, 10), OrderFact("o2", 4, 4)],
    )
    assert r.ok, r.diffs


@pytest.mark.parametrize(
    ("kw", "kind"),
    [
        ({"positions": [PositionFact("INTRADAY", KEY, 7)]}, "POSITION_MISMATCH"),
        ({"positions": [PositionFact("SWING", KEY, 10)]}, "POSITION_MISMATCH"),
        ({"cash": Decimal(1)}, "CASH_MISMATCH"),
        ({"orders": [OrderFact("o1", 10, 9)]}, "ORDER_FILL_MISMATCH"),
        ({"orders": []}, "ORPHAN_FILL"),
    ],
)
def test_reconcile_breaks(kw: dict[str, object], kind: str) -> None:
    base: dict[str, object] = {
        "starting_cash": Decimal(10_000),
        "cash": Decimal(10_000) - 1000 - 1,
        "positions": [PositionFact("INTRADAY", KEY, 10)],
        "fills": [_fill("BUY", 10)],
        "orders": [OrderFact("o1", 10, 10)],
    }
    base.update(kw)
    r = reconcile_paper(**base)  # type: ignore[arg-type]
    assert not r.ok and kind in {d.kind for d in r.diffs}


def test_negative_position_in_journal_is_a_break() -> None:
    r = reconcile_paper(
        starting_cash=Decimal(10_000),
        cash=Decimal(10_000) + 1000 - 1,
        positions=[],
        fills=[_fill("SELL", 10)],
        orders=[OrderFact("o1", 10, 10)],
    )
    assert "NEGATIVE_POSITION" in {d.kind for d in r.diffs}


def test_consolidation_and_venue_invariant() -> None:
    ledgers = [
        PositionFact("INTRADAY", KEY, 10),
        PositionFact("LONG_TERM", KEY, 40),
        PositionFact("SWING", OTHER, 0),
    ]
    assert consolidate(ledgers) == {KEY: 50}
    assert reconcile_with_venue(ledgers, {KEY: 50}).ok
    bad = reconcile_with_venue(ledgers, {KEY: 45, OTHER: 5})
    assert {(d.subject, d.expected, d.actual) for d in bad.diffs} == {
        (str(KEY), "50", "45"),
        (str(OTHER), "0", "5"),
    }
