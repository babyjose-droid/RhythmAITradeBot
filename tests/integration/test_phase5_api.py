"""Phase 5 end to end on PostgreSQL: event calendar, swing PAPER cycles across sessions,
theses with append-only history, swing backtests. MOCK data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from rhythm.api.app import create_app
from rhythm.broker.mock import MockBrokerAdapter
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.db.session import create_session_factory, session_scope
from rhythm.events.mock import MockEventProvider
from rhythm.marketdata.mock import MockMarketDataProvider
from tests.conftest import auto_idempotency, make_settings

pytestmark = pytest.mark.integration

CAL = SessionCalendar()
SWING = "swing_trend_pullback"
# A MOCK session where the swing baseline enters and then holds for several sessions.
SYMBOL = "NSE:MOCKB"
ENTRY_DAY = date(2026, 10, 8)


def after_close(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=EXCHANGE_TZ)


def next_session(d: date) -> date:
    n = d + timedelta(days=1)
    while not CAL.is_trading_day(n):
        n += timedelta(days=1)
    return n


class Env5:
    def __init__(self, db_url: str, engine: Engine, *, events: bool = True) -> None:
        self.clock = FixedClock(after_close(ENTRY_DAY))
        self.engine = engine
        app = create_app(
            make_settings(
                database_url=db_url,
                market_data_provider="mock",
                broker_adapter="mock",
                event_provider="mock" if events else "none",
                trading_mode="PAPER",
                features={"paper_trading": True},
            ),
            engine=engine,
            clock=self.clock,
            calendar=CAL,
            market_data=MockMarketDataProvider(clock=self.clock, calendar=CAL),
            broker=MockBrokerAdapter(clock=self.clock),
            events=MockEventProvider() if events else None,
            configure_logs=False,
        )
        self.c = TestClient(app)
        auto_idempotency(self.c)

    def setup(self, name: str = "swing") -> tuple[int, int]:
        assert self.c.post("/v1/instruments/sync").status_code == 200
        wid = self.c.post("/v1/watchlists", json={"name": name, "horizon": "SWING"}).json()["id"]
        assert (
            self.c.post(f"/v1/watchlists/{wid}/items", json={"instrument": SYMBOL}).status_code
            == 201
        )
        aid = self.c.post("/v1/paper/accounts", json={"name": name}).json()["id"]
        return wid, aid

    def cycle(self, aid: int, wid: int) -> Any:
        r = self.c.post(
            f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid, "strategy": SWING}
        )
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def env5(db_url: str, migrated: Engine) -> Iterator[Env5]:
    e = Env5(db_url, migrated)
    with e.c:
        yield e


def test_event_sync_listing_and_manual_entry(env5: Env5) -> None:
    c = env5.c
    wid, _ = env5.setup()
    before = c.get(f"/v1/events?watchlist_id={wid}").json()
    assert before["events"] == [] and before["last_sync"] == {SYMBOL: None}
    s = c.post("/v1/events/sync", json={"watchlist_id": wid}).json()
    assert s["source"] == "MOCK" and s["instruments"] == 1
    listing = c.get(f"/v1/events?watchlist_id={wid}&days=120").json()
    assert listing["last_sync"][SYMBOL] is not None
    assert all(e["is_mock"] and e["kind"] == "RESULTS" for e in listing["events"])
    # every listed event was already public at sync time (no look-ahead)
    now = after_close(ENTRY_DAY)
    assert all(datetime.fromisoformat(e["known_at"]) <= now for e in listing["events"])

    manual = {
        "instrument": SYMBOL,
        "kind": "DIVIDEND",
        "event_date": "2026-11-20",
        "source_ref": "exchange notice (test)",
    }
    assert c.post("/v1/events", json=manual).json()["added"] is True
    assert c.post("/v1/events", json=manual).json()["added"] is False  # idempotent
    listed = c.get(f"/v1/events?watchlist_id={wid}&days=120").json()["events"]
    (div,) = [e for e in listed if e["kind"] == "DIVIDEND"]
    assert div["source"] == "MANUAL" and div["is_mock"] is False and div["days_away"] == 43
    future = manual | {"known_at": "2026-12-01T00:00:00+05:30"}
    assert c.post("/v1/events", json=future).status_code == 422


def test_swing_entry_blocked_until_calendar_synced(env5: Env5) -> None:
    wid, aid = env5.setup()
    r = env5.cycle(aid, wid)
    (sig,) = [s for s in r["signals"] if s["type"] == "ENTER_LONG"]
    assert sig["order_id"] is None and sig["failed_checks"] == ["R18"]
    assert "never synced" in r["skipped"][SYMBOL]


def test_swing_multi_day_paper_lifecycle_with_thesis(env5: Env5) -> None:
    c = env5.c
    wid, aid = env5.setup()
    c.post("/v1/events/sync", json={"watchlist_id": wid})

    # Day 0 (after the close): decision on today's daily bar -> order at the venue + thesis
    r0 = env5.cycle(aid, wid)
    assert r0["horizon"] == "SWING" and r0["processed_bars"] == {SYMBOL: 1}
    (sig,) = [s for s in r0["signals"] if s["type"] == "ENTER_LONG"]
    assert sig["order_id"] and r0["theses"]["created"] == 1
    (order,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert order["horizon"] == "SWING" and order["state"] == "BROKER_ACKNOWLEDGED"
    (th,) = c.get(f"/v1/theses?account_id={aid}").json()
    assert th["status"] == "PENDING_ENTRY" and th["entry_order_id"] == order["id"]
    assert th["rationale"] and {r["rule"] for r in th["invalidation"]} >= {
        "stop",
        "close_below_sma",
    }
    checks = c.get(f"/v1/risk/checks?order_id={order['id']}").json()
    r18 = next(x for x in checks[0]["checks"] if x["code"] == "R18")
    assert r18["status"] == "PASS"

    # Day 1: entry fills at the open; protection is placed; at the close the trend breaks,
    # so the strategy's thesis check fails -> exit order queued, thesis INVALIDATED.
    d = next_session(ENTRY_DAY)
    env5.clock.set(after_close(d))
    r1 = env5.cycle(aid, wid)
    assert r1["new_fills"] == 1 and r1["theses"]["opened"] == 1
    assert r1["theses"]["invalidated"] == 1
    (ex,) = [s for s in r1["signals"] if s["type"] == "EXIT_LONG"]
    assert "thesis invalidated" in ex["reasons"][0] and ex["order_id"]
    ledgers = {
        lg["horizon"]: lg
        for lg in c.get(f"/v1/portfolio/ledgers?account_id={aid}").json()["ledgers"]
    }
    assert ledgers["SWING"]["open_positions"] == 1 and ledgers["INTRADAY"]["open_positions"] == 0
    working = [
        o
        for o in c.get(f"/v1/paper/accounts/{aid}/orders").json()
        if o["state"] == "BROKER_ACKNOWLEDGED"
    ]
    assert [o["purpose"] for o in working] == ["exit"]  # protection cancelled for the exit
    mid = c.get(f"/v1/theses/{th['id']}").json()
    assert mid["status"] == "INVALIDATED" and mid["last_check"]["exit_reasons"]

    # Next sessions: the exit fills at the open. Fills are capped at 10 % of the session's
    # volume, so a thin MOCK session can leave a remainder that is re-sent the next day.
    # The thesis closes only when the position is flat.
    sessions = 0
    while True:
        d = next_session(d)
        env5.clock.set(after_close(d))
        env5.cycle(aid, wid)
        sessions += 1
        cur = c.get(f"/v1/theses/{th['id']}").json()
        if cur["status"] == "CLOSED" or sessions >= 10:
            break
        assert cur["status"] == "INVALIDATED" and cur["last_checked_at"] is not None
    full = c.get(f"/v1/theses/{th['id']}").json()
    assert full["status"] == "CLOSED" and full["close_reason"] == "position closed by exit"
    assert [e["to"] for e in full["events"]] == ["PENDING_ENTRY", "OPEN", "INVALIDATED", "CLOSED"]
    inv = full["events"][2]
    assert inv["reason"].startswith("thesis invalidated") and inv["detail"]["exit_order_id"]
    exits = [o for o in c.get(f"/v1/paper/accounts/{aid}/orders").json() if o["purpose"] == "exit"]
    entry_qty = next(
        o["filled_quantity"]
        for o in c.get(f"/v1/paper/accounts/{aid}/orders").json()
        if o["purpose"] == "entry"
    )
    assert sum(o["filled_quantity"] for o in exits) == entry_qty  # sold exactly what was bought
    assert c.get(f"/v1/portfolio/consolidated?account_id={aid}").json()["positions"] == []

    # Day 3: nothing open, nothing to do; the closed thesis is not touched again
    d = next_session(d)
    env5.clock.set(after_close(d))
    env5.cycle(aid, wid)
    assert c.get(f"/v1/theses/{th['id']}").json()["closed_at"] == full["closed_at"]
    # reconciliation stayed clean across all sessions
    assert all(
        x["status"] == "OK" for x in c.get(f"/v1/reconciliations?account_id={aid}&limit=500").json()
    )
    assert c.get("/v1/audit/verify").json()["ok"] is True
    with (
        pytest.raises(DBAPIError, match="append-only"),
        session_scope(create_session_factory(env5.engine)) as s,
    ):
        s.execute(text("UPDATE thesis_events SET reason = 'x'"))


def test_swing_research_cycle_creates_no_orders_or_theses(env5: Env5) -> None:
    c = env5.c
    wid, _ = env5.setup()
    c.post("/v1/events/sync", json={"watchlist_id": wid})
    res = c.post("/v1/research/cycle", json={"watchlist_id": wid, "strategy": SWING}).json()
    assert res["mode"] == "RESEARCH" and res["horizon"] == "SWING"
    assert any(s["decision"] == "BUY_CANDIDATE" for s in res["signals"])
    assert all(s["order_id"] is None for s in res["signals"])
    assert res["theses"] == {}


def test_swing_backtest_api(env5: Env5) -> None:
    c = env5.c
    body = {
        "strategy": SWING,
        "symbols": [SYMBOL, "NSE:MOCKA"],
        "start": "2025-09-01T00:00:00+05:30",
        "end": "2026-10-01T00:00:00+05:30",
        "data_source": "provider",
        "splits": [
            {
                "name": "development",
                "start": "2025-09-01T00:00:00+05:30",
                "end": "2026-04-01T00:00:00+05:30",
            },
            {
                "name": "validation",
                "start": "2026-04-01T00:00:00+05:30",
                "end": "2026-10-01T00:00:00+05:30",
            },
        ],
    }
    r = c.post("/v1/backtests", json=body)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["interval"] == "1d" and run["data_is_mock"] is True and run["trades"] > 0
    assert set(run["splits"]) == {"development", "validation"}


def test_swing_backtest_without_event_source_cannot_enter(db_url: str, migrated: Engine) -> None:
    e = Env5(db_url, migrated, events=False)
    with e.c as c:
        body = {
            "strategy": SWING,
            "symbols": [SYMBOL],
            "start": "2025-09-01T00:00:00+05:30",
            "end": "2026-10-01T00:00:00+05:30",
            "data_source": "provider",
        }
        run = c.post("/v1/backtests", json=body).json()
        assert run["trades"] == 0  # R18: no event coverage -> no swing entries (fail closed)
        assert c.post("/v1/events/sync", json={"watchlist_id": 1}).status_code in (404, 503)
