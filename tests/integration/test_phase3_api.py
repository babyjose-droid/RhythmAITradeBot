"""PAPER trading and backtest API on MOCK data, end to end against PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from rhythm.api.app import create_app
from rhythm.broker.mock import MOCK_SYMBOLS, MockBrokerAdapter
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.db.models import OrderRow
from rhythm.db.session import create_session_factory, session_scope
from rhythm.execution.book import PaperBook
from rhythm.execution.runner import IntradayRunner
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.types import Interval
from rhythm.strategies.base import DecisionState
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

CAL = SessionCalendar()
DAY_OPEN = datetime(2026, 10, 30, 9, 15, tzinfo=EXCHANGE_TZ)
DAY_END = datetime(2026, 10, 30, 15, 31, tzinfo=EXCHANGE_TZ)


def first_entry() -> tuple[InstrumentKey, datetime]:
    """Find a deterministic MOCK instrument/time where the baseline emits its first entry."""
    prov = MockMarketDataProvider(clock=FixedClock(DAY_END), calendar=CAL)
    for sym in MOCK_SYMBOLS:
        key = InstrumentKey(Exchange.NSE, sym)
        runner = IntradayRunner(IntradayOrbVwap(), PaperBook(cash=Decimal(1_000_000)), CAL)
        for b in prov.bars(key, Interval.M5, DAY_OPEN, DAY_END):
            for rec in runner.on_bar(b)[1]:
                if rec.decision is DecisionState.BUY_CANDIDATE:
                    return key, rec.signal.at
    raise AssertionError("no entry found in mock data")


KEY, SIGNAL_AT = first_entry()


class Env:
    def __init__(self, db_url: str, engine: Engine, *, paper: bool) -> None:
        self.clock = FixedClock(SIGNAL_AT + timedelta(seconds=2))
        extra: dict[str, Any] = (
            {"trading_mode": "PAPER", "features": {"paper_trading": True}} if paper else {}
        )
        self.engine = engine
        app = create_app(
            make_settings(
                database_url=db_url, market_data_provider="mock", broker_adapter="mock", **extra
            ),
            engine=engine,
            clock=self.clock,
            calendar=CAL,
            market_data=MockMarketDataProvider(clock=self.clock, calendar=CAL),
            broker=MockBrokerAdapter(clock=self.clock),
            configure_logs=False,
        )
        self.c = TestClient(app)

    def setup_watchlist(self) -> int:
        assert self.c.post("/v1/instruments/sync").status_code == 200
        wid: int = self.c.post("/v1/watchlists", json={"name": "p3", "horizon": "INTRADAY"}).json()[
            "id"
        ]
        assert (
            self.c.post(f"/v1/watchlists/{wid}/items", json={"instrument": str(KEY)}).status_code
            == 201
        )
        return wid


@pytest.fixture
def paper_env(db_url: str, migrated: Engine) -> Iterator[Env]:
    e = Env(db_url, migrated, paper=True)
    with e.c:
        yield e


def test_paper_lifecycle_entry_fill_protection_square_off(paper_env: Env) -> None:
    c = paper_env.c
    wid = paper_env.setup_watchlist()
    acct = c.post("/v1/paper/accounts", json={"name": "demo", "starting_cash": "1000000"})
    assert acct.status_code == 201 and acct.headers["x-account-kind"] == "PAPER"
    aid = acct.json()["id"]

    # cycle 1: decision on the signal bar -> PAPER order at the simulated venue, not filled
    r1 = c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid}).json()
    assert r1["mode"] == "PAPER" and r1["data_is_mock"] is True and r1["new_orders"] == 1
    (sig,) = [s for s in r1["signals"] if s["decision"] == "BUY_CANDIDATE"]
    (order,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert order["id"] == sig["order_id"] and order["account_kind"] == "PAPER"
    assert order["state"] == "BROKER_ACKNOWLEDGED" and order["display"] == "AT_BROKER_NOT_FILLED"
    assert c.get(f"/v1/paper/accounts/{aid}/fills").json() == []
    signals = c.get(f"/v1/signals?account_id={aid}").json()
    assert signals[0]["kind"] == "CANDIDATE" and signals[0]["order_id"] == order["id"]

    # cycle 2: next bar -> entry fills at that bar, protection attached (or stopped out same bar)
    paper_env.clock.advance(timedelta(minutes=5))
    r2 = c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid}).json()
    assert r2["new_fills"] >= 1
    orders = {o["purpose"]: o for o in c.get(f"/v1/paper/accounts/{aid}/orders").json()}
    assert orders["entry"]["state"] == "FILLED" and orders["entry"]["display"] == "FILLED"
    fill = c.get(f"/v1/paper/accounts/{aid}/fills").json()[-1]
    assert datetime.fromisoformat(fill["at"]) == SIGNAL_AT  # fills at the open of the next bar
    detail = c.get(f"/v1/paper/accounts/{aid}").json()
    if detail["positions"]:
        assert {"stop", "target"} <= set(orders)
        assert Decimal(detail["cash"]) < Decimal("1000000")

    # cycle 3: after the close -> nothing left open
    paper_env.clock.set(DAY_END)
    c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid})
    detail = c.get(f"/v1/paper/accounts/{aid}").json()
    assert detail["positions"] == []
    states = {o["state"] for o in c.get(f"/v1/paper/accounts/{aid}/orders").json()}
    assert states <= {"FILLED", "CANCELLED", "EXPIRED"}
    n_orders = len(c.get(f"/v1/paper/accounts/{aid}/orders").json())

    # re-running the same cycle is idempotent
    again = c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid}).json()
    assert again["new_orders"] == 0 and again["new_fills"] == 0
    assert len(c.get(f"/v1/paper/accounts/{aid}/orders").json()) == n_orders

    events = c.get(f"/v1/paper/orders/{order['id']}/events").json()
    assert [e["to"] for e in events][:4] == [
        "PROPOSED",
        "RISK_CHECKED",
        "SUBMITTED",
        "BROKER_ACKNOWLEDGED",
    ]
    assert c.get("/v1/audit/verify").json()["ok"] is True


def test_db_enforces_paper_only_and_append_only(paper_env: Env) -> None:
    c = paper_env.c
    wid = paper_env.setup_watchlist()
    aid = c.post("/v1/paper/accounts", json={"name": "x"}).json()["id"]
    c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid})
    sf = create_session_factory(paper_env.engine)
    with pytest.raises(IntegrityError), session_scope(sf) as s:
        s.execute(text("UPDATE orders SET account_kind = 'LIVE'"))
    with pytest.raises(DBAPIError, match="append-only"), session_scope(sf) as s:
        s.execute(text("DELETE FROM order_events"))


def test_cancel_working_order(paper_env: Env) -> None:
    c = paper_env.c
    wid = paper_env.setup_watchlist()
    aid = c.post("/v1/paper/accounts", json={"name": "y"}).json()["id"]
    c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid})
    (o,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    r = c.post(f"/v1/paper/orders/{o['id']}/cancel").json()
    assert r["state"] == "CANCELLED" and r["display"] == "CLOSED_WITHOUT_FILL"
    assert c.post(f"/v1/paper/orders/{o['id']}/cancel").status_code == 409


def test_research_mode_never_creates_orders(db_url: str, migrated: Engine) -> None:
    e = Env(db_url, migrated, paper=False)
    with e.c as c:
        wid = e.setup_watchlist()
        aid = c.post("/v1/paper/accounts", json={"name": "r"}).json()["id"]
        assert (
            c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid}).status_code == 409
        )
        res = c.post("/v1/research/cycle", json={"watchlist_id": wid}).json()
        assert res["mode"] == "RESEARCH" and any(
            s["decision"] == "BUY_CANDIDATE" for s in res["signals"]
        )
        assert all(s["order_id"] is None for s in res["signals"])
        sigs = c.get("/v1/signals?context=RESEARCH").json()
        assert sigs and all(s["order_id"] is None for s in sigs)
        assert sigs[0]["risk"]["note"] == "research only: no order created"
    with session_scope(create_session_factory(migrated)) as s:
        assert s.execute(select(func.count()).select_from(OrderRow)).scalar_one() == 0


def test_catch_up_bars_never_open_positions(paper_env: Env) -> None:
    """A late first cycle replays the day, but entries are allowed only on the latest bar."""
    c = paper_env.c
    wid = paper_env.setup_watchlist()
    aid = c.post("/v1/paper/accounts", json={"name": "late"}).json()["id"]
    paper_env.clock.set(SIGNAL_AT + timedelta(minutes=60, seconds=2))
    res = c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid}).json()
    buys = [s for s in res["signals"] if s["type"] == "ENTER_LONG"]
    assert buys and all(s["decision"] == "REJECTED_BY_RISK" for s in buys[:-1] or buys)
    assert res["processed_bars"][str(KEY)] > 10


def test_backtest_api(paper_env: Env) -> None:
    c = paper_env.c
    end = DAY_END.isoformat()
    start = (DAY_END - timedelta(days=10)).isoformat()
    mid = (DAY_END - timedelta(days=5)).isoformat()
    body = {
        "symbols": ["NSE:MOCKA", "NSE:MOCKB"],
        "start": start,
        "end": end,
        "data_source": "provider",
        "splits": [
            {"name": "dev", "start": start, "end": mid},
            {"name": "oos", "start": mid, "end": end},
        ],
    }
    r = c.post("/v1/backtests", json=body)
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["data_is_mock"] is True and set(run["splits"]) == {"dev", "oos"}
    assert run["cost_model"]["verified"] is False
    again = c.post("/v1/backtests", json=body).json()
    assert again["run_hash"] == run["run_hash"] and again["metrics"] == run["metrics"]
    detail = c.get(f"/v1/backtests/{run['id']}").json()
    assert len([t for t in detail["trade_list"] if t["split"] == "full"]) == run["trades"]
    assert c.get("/v1/backtests").json()[0]["id"] == again["id"]
    assert (
        c.post("/v1/backtests", json={**body, "data_source": "db", "splits": []}).status_code == 422
    )
    assert c.get("/v1/strategies").json()[0]["name"] == "intraday_orb_vwap"
