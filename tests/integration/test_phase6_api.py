"""Phase 6 end to end on PostgreSQL: fundamentals sync, long-term PAPER cycles with theses,
scheduled/results-triggered reviews (valuation history), allocation band. MOCK data only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rhythm.api.app import create_app
from rhythm.broker.mock import MockBrokerAdapter
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.events.mock import MockEventProvider
from rhythm.fundamentals.mock import MockFundamentalsProvider
from rhythm.marketdata.mock import MockMarketDataProvider
from tests.conftest import auto_idempotency, make_settings

pytestmark = pytest.mark.integration

CAL = SessionCalendar()
LT = "long_term_quality_value"
QUALITY = "NSE:MOCKE"  # passes the MOCK quality/valuation screen
WEAK = "NSE:MOCKA"  # ROE below the screen's minimum
START = date(2026, 10, 20)  # after the MOCK results date of MOCKE (event gate)


def after_close(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 16, 0, tzinfo=EXCHANGE_TZ)


def next_session(d: date) -> date:
    n = d + timedelta(days=1)
    while not CAL.is_trading_day(n):
        n += timedelta(days=1)
    return n


class Env6:
    def __init__(self, db_url: str, engine: Engine, *, fundamentals: bool = True) -> None:
        self.clock = FixedClock(after_close(START))
        market = MockMarketDataProvider(clock=self.clock, calendar=CAL)
        app = create_app(
            make_settings(
                database_url=db_url,
                market_data_provider="mock",
                broker_adapter="mock",
                event_provider="mock",
                fundamentals_provider="mock" if fundamentals else "none",
                trading_mode="PAPER",
                features={"paper_trading": True},
            ),
            engine=engine,
            clock=self.clock,
            calendar=CAL,
            market_data=market,
            broker=MockBrokerAdapter(clock=self.clock),
            events=MockEventProvider(),
            fundamentals=MockFundamentalsProvider(market.price_at) if fundamentals else None,
            configure_logs=False,
        )
        self.c = TestClient(app)
        auto_idempotency(self.c)

    def setup(self) -> tuple[int, int]:
        assert self.c.post("/v1/instruments/sync").status_code == 200
        wid = self.c.post("/v1/watchlists", json={"name": "lt", "horizon": "LONG_TERM"}).json()[
            "id"
        ]
        for s in (QUALITY, WEAK):
            assert (
                self.c.post(f"/v1/watchlists/{wid}/items", json={"instrument": s}).status_code
                == 201
            )
        aid = self.c.post("/v1/paper/accounts", json={"name": "lt"}).json()["id"]
        return wid, aid

    def cycle(self, aid: int, wid: int) -> Any:
        r = self.c.post(
            f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid, "strategy": LT}
        )
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def env6(db_url: str, migrated: Engine) -> Iterator[Env6]:
    e = Env6(db_url, migrated)
    with e.c:
        yield e


def test_fundamentals_sync_and_point_in_time_listing(env6: Env6) -> None:
    c = env6.c
    wid, _ = env6.setup()
    s = c.post("/v1/fundamentals/sync", json={"watchlist_id": wid}).json()
    assert s["source"] == "MOCK" and s["added"][QUALITY] > 8
    again = c.post("/v1/fundamentals/sync", json={"watchlist_id": wid}).json()
    assert again["added"] == {QUALITY: 0, WEAK: 0}  # nothing rewritten
    f = c.get("/v1/fundamentals/NSE/MOCKE?quarters=4").json()
    assert f["is_mock"] is True and len(f["quarters"]) == 4
    assert f["quarters"][-1]["period_end"] == "2026-06-30"  # Sep quarter not published yet
    assert all(datetime.fromisoformat(q["known_at"]) <= after_close(START) for q in f["quarters"])
    assert f["derived"]["pe"]["formula"] == "price / eps_ttm" and f["derived"]["roe"]["value"]


def test_long_term_entry_blocked_without_fundamentals(db_url: str, migrated: Engine) -> None:
    e = Env6(db_url, migrated, fundamentals=False)
    with e.c as c:
        wid, aid = e.setup()
        c.post("/v1/events/sync", json={"watchlist_id": wid})
        r = e.cycle(aid, wid)
        assert r["new_orders"] == 0 and "no published fundamentals" in r["skipped"][QUALITY]
        assert c.post("/v1/fundamentals/sync", json={"watchlist_id": wid}).status_code == 503


def test_long_term_lifecycle_with_reviews_and_valuation_history(env6: Env6) -> None:
    c = env6.c
    wid, aid = env6.setup()
    c.post("/v1/fundamentals/sync", json={"watchlist_id": wid})
    c.post("/v1/events/sync", json={"watchlist_id": wid})

    r0 = env6.cycle(aid, wid)
    assert r0["horizon"] == "LONG_TERM"
    entries = [s for s in r0["signals"] if s["type"] == "ENTER_LONG"]
    assert [s["instrument"] for s in entries] == [QUALITY] and entries[0]["order_id"]
    (order,) = c.get(f"/v1/paper/accounts/{aid}/orders?horizon=LONG_TERM").json()
    ledger = {
        lg["horizon"]: lg
        for lg in c.get(f"/v1/portfolio/ledgers?account_id={aid}").json()["ledgers"]
    }
    target_value = Decimal("0.10") * Decimal(ledger["LONG_TERM"]["allocated_capital"])
    price = Decimal(c.get("/v1/fundamentals/NSE/MOCKE").json()["price"])
    assert abs(order["quantity"] * price - target_value) < price * 2  # 10 % target weight
    (th,) = c.get(f"/v1/theses?account_id={aid}").json()
    assert th["status"] == "PENDING_ENTRY" and {r["rule"] for r in th["invalidation"]} >= {
        "roe_below"
    }
    first = c.get(f"/v1/theses/{th['id']}").json()["events"][0]
    assert first["detail"]["key_metrics"]["pe"]["formula"] == "price / eps_ttm"
    assert first["detail"]["evidence"][0]["source"] == "MOCK"

    # Next session: the entry fills; no stop/target orders for long-term positions.
    d = next_session(START)
    env6.clock.set(after_close(d))
    env6.cycle(aid, wid)
    assert c.get(f"/v1/theses/{th['id']}").json()["status"] == "OPEN"
    working = [
        o
        for o in c.get(f"/v1/paper/accounts/{aid}/orders").json()
        if o["state"] == "BROKER_ACKNOWLEDGED"
    ]
    assert working == []

    # Run daily until the September quarter is published (mid-November): that triggers a
    # review, recorded in the thesis history with the metrics used (valuation history).
    reviews: list[dict[str, Any]] = []
    for _ in range(30):
        d = next_session(d)
        env6.clock.set(after_close(d))
        c.post("/v1/fundamentals/sync", json={"watchlist_id": wid})
        if d.weekday() == 0:
            c.post("/v1/events/sync", json={"watchlist_id": wid})  # weekly calendar refresh
        env6.cycle(aid, wid)
        reviews = [
            e
            for e in c.get(f"/v1/theses/{th['id']}").json()["events"]
            if e["reason"].startswith("review")
        ]
        if any(e["detail"].get("new_results") for e in reviews):
            break
    assert reviews, "no review recorded"
    rv = next(e for e in reviews if e["detail"].get("new_results"))
    assert rv["detail"]["latest_period"] == "2026-09-30" and rv["detail"]["metrics"]["pe"]["value"]
    assert rv["detail"]["band"] == ["0.05", "0.10", "0.15"] and rv["from"] == rv["to"] == "OPEN"
    cur = c.get(f"/v1/theses/{th['id']}").json()
    assert cur["status"] == "OPEN" and cur["last_check"]["latest_period"] == "2026-09-30"
    # the weak name never got an order, and reconciliation stayed clean throughout
    assert all(o["instrument"] == QUALITY for o in c.get(f"/v1/paper/accounts/{aid}/orders").json())
    assert all(
        x["status"] == "OK" for x in c.get(f"/v1/reconciliations?account_id={aid}&limit=500").json()
    )
    assert c.get("/v1/audit/verify").json()["ok"] is True


def test_long_term_backtest_api(env6: Env6) -> None:
    body = {
        "strategy": LT,
        "symbols": [QUALITY, WEAK],
        "start": "2025-06-01T00:00:00+05:30",
        "end": "2026-10-01T00:00:00+05:30",
        "data_source": "provider",
    }
    r = env6.c.post("/v1/backtests", json=body)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["interval"] == "1d" and run["data_is_mock"] is True
    assert run["metrics"]["positions_open_at_end"] >= 1
