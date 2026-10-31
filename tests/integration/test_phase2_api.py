"""End-to-end API tests for instruments, watchlists and market data (MOCK sources only)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from rhythm.api.app import create_app
from rhythm.broker.mock import MockBrokerAdapter, MockBrokerScenario
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.db.models import AuditEvent, DataQualityEvent
from rhythm.db.session import create_session_factory, session_scope
from rhythm.marketdata.mock import MockMarketDataProvider, MockScenario
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

IN_SESSION = datetime(2026, 10, 30, 11, 0, tzinfo=EXCHANGE_TZ)  # Friday
A = InstrumentKey(Exchange.NSE, "MOCKA")


class Env:
    def __init__(self, db_url: str, engine: Engine, at: datetime = IN_SESSION) -> None:
        self.clock = FixedClock(at)
        self.cal = SessionCalendar()
        self.scenario = MockScenario()
        self.broker_scenario = MockBrokerScenario()
        self.engine = engine
        app = create_app(
            make_settings(database_url=db_url, market_data_provider="mock", broker_adapter="mock"),
            engine=engine,
            clock=self.clock,
            calendar=self.cal,
            market_data=MockMarketDataProvider(
                clock=self.clock, calendar=self.cal, scenario=self.scenario
            ),
            broker=MockBrokerAdapter(clock=self.clock, scenario=self.broker_scenario),
            configure_logs=False,
        )
        self.client = TestClient(app)

    def events(self, model: type, col: str) -> list[str]:
        with session_scope(create_session_factory(self.engine)) as s:
            return list(s.execute(select(getattr(model, col)).order_by(model.id)).scalars())  # type: ignore[attr-defined]


@pytest.fixture
def env(db_url: str, migrated: Engine) -> Iterator[Env]:
    e = Env(db_url, migrated)
    with e.client:
        yield e


def _sync(env: Env) -> dict:  # type: ignore[type-arg]
    r = env.client.post("/v1/instruments/sync?exchange=NSE")
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def _watchlist(env: Env, horizon: str = "INTRADAY", n: int = 3) -> int:
    r = env.client.post("/v1/watchlists", json={"name": f"wl-{horizon}", "horizon": horizon})
    assert r.status_code == 201, r.text
    wid: int = r.json()["id"]
    for sym in ["MOCKA", "MOCKB", "MOCKC"][:n]:
        assert (
            env.client.post(
                f"/v1/watchlists/{wid}/items", json={"instrument": f"NSE:{sym}"}
            ).status_code
            == 201
        )
    return wid


# ------------------------------------------------------------------ instruments
def test_sync_and_list_instruments(env: Env) -> None:
    body = _sync(env)
    assert (
        body["status"] == "APPLIED" and body["is_mock"] is True and body["summary"]["added"] == 20
    )
    assert _sync(env)["already_applied"] is True
    rows = env.client.get("/v1/instruments?exchange=NSE&q=MOCKA").json()
    assert [r["symbol"] for r in rows] == ["MOCKA"]
    assert rows[0]["is_mock"] is True and rows[0]["tick_size"] == "0.05"
    assert env.client.get("/v1/instruments/NSE/MOCKA").status_code == 200
    assert env.client.get("/v1/instruments/NSE/NOPE").status_code == 404
    assert env.client.get("/v1/instruments/snapshots").json()[0]["status"] == "APPLIED"


def test_sync_fails_closed_when_broker_login_expired(env: Env) -> None:
    env.broker_scenario.auth_expired = True
    r = env.client.post("/v1/instruments/sync")
    assert r.status_code == 503
    assert "login required" in r.json()["detail"]


def test_rejected_sync_returns_422_and_persists_record(env: Env) -> None:
    _sync(env)
    env.clock.advance(timedelta(days=1))
    master = list(MockBrokerAdapter().get_scrip_master(Exchange.NSE))
    env.broker_scenario.scrip_master_override = master[:3]
    r = env.client.post("/v1/instruments/sync")
    assert r.status_code == 422 and "too many removals" in r.json()["detail"]
    assert "SNAPSHOT_REJECTED" in env.events(DataQualityEvent, "kind")


# ------------------------------------------------------------------ watchlists
def test_watchlist_crud_is_audited(env: Env) -> None:
    _sync(env)
    wid = _watchlist(env)
    assert (
        env.client.post(
            "/v1/watchlists", json={"name": "wl-INTRADAY", "horizon": "SWING"}
        ).status_code
        == 409
    )
    assert (
        env.client.post("/v1/watchlists", json={"name": "x", "horizon": "WEEKLY"}).status_code
        == 422
    )
    assert (
        env.client.post(f"/v1/watchlists/{wid}/items", json={"instrument": "NSE:MOCKA"}).status_code
        == 409
    )
    assert (
        env.client.post(
            f"/v1/watchlists/{wid}/items", json={"instrument": "NSE:REALCO"}
        ).status_code
        == 404
    )
    assert (
        env.client.post(f"/v1/watchlists/{wid}/items", json={"instrument": "bad"}).status_code
        == 422
    )
    got = env.client.get(f"/v1/watchlists/{wid}").json()
    assert [i["instrument"] for i in got["items"]] == ["NSE:MOCKA", "NSE:MOCKB", "NSE:MOCKC"]
    assert all(i["active_in_registry"] for i in got["items"])
    assert env.client.delete(f"/v1/watchlists/{wid}/items/NSE/MOCKB").status_code == 204
    assert env.client.delete(f"/v1/watchlists/{wid}/items/NSE/MOCKB").status_code == 404
    assert env.client.delete(f"/v1/watchlists/{wid}").status_code == 204
    assert env.client.get(f"/v1/watchlists/{wid}").status_code == 404
    types = env.events(AuditEvent, "event_type")
    assert types.count("watchlist.item_added") == 3
    assert {"watchlist.created", "watchlist.item_removed", "watchlist.deleted"} <= set(types)
    assert env.client.get("/v1/audit/verify").json()["ok"] is True


# ------------------------------------------------------------------ market data
def test_quote_endpoint_labels_mock_and_freshness(env: Env) -> None:
    body = env.client.get("/v1/marketdata/quote/NSE/MOCKA").json()
    assert body["is_mock"] is True and body["source"] == "MOCK"
    assert body["freshness"]["status"] == "FRESH"
    assert body["freshness"]["usable_for_new_orders"] is True
    env.scenario.stale[A] = timedelta(minutes=5)
    stale = env.client.get("/v1/marketdata/quote/NSE/MOCKA").json()
    assert stale["freshness"]["status"] == "STALE"
    assert stale["freshness"]["usable_for_new_orders"] is False
    env.scenario.errors.add(A)
    assert env.client.get("/v1/marketdata/quote/NSE/MOCKA").status_code == 503


def test_backfill_and_read_bars(env: Env) -> None:
    r = env.client.post("/v1/marketdata/bars/NSE/MOCKA/backfill?interval=1d&days=10").json()
    assert r["is_mock"] is True and r["written"] == 7 and r["rejected"] == 0
    again = env.client.post("/v1/marketdata/bars/NSE/MOCKA/backfill?interval=1d&days=10").json()
    assert again["written"] == 7  # idempotent upsert
    bars = env.client.get("/v1/marketdata/bars/NSE/MOCKA?interval=1d").json()
    assert len(bars) == 7 and all(b["is_mock"] for b in bars)
    starts = [b["start"] for b in bars]
    assert starts == sorted(starts)
    assert (
        env.client.get("/v1/marketdata/bars/NSE/MOCKA?start=2026-10-01T00:00:00").status_code == 422
    )


def test_intraday_freshness_records_transitions_once(env: Env) -> None:
    _sync(env)
    wid = _watchlist(env)
    first = env.client.get(f"/v1/marketdata/freshness?watchlist_id={wid}").json()
    assert {r["freshness"]["status"] for r in first} == {"FRESH"}
    env.scenario.stale[A] = timedelta(minutes=5)
    for _ in range(3):
        res = env.client.get(f"/v1/marketdata/freshness?watchlist_id={wid}").json()
    by = {r["instrument"]: r["freshness"]["status"] for r in res}
    assert by == {"NSE:MOCKA": "STALE", "NSE:MOCKB": "FRESH", "NSE:MOCKC": "FRESH"}
    env.scenario.stale.clear()
    env.client.get(f"/v1/marketdata/freshness?watchlist_id={wid}")
    kinds = env.events(DataQualityEvent, "kind")
    assert kinds == ["STALE", "RECOVERED"]  # one event per transition, not per poll
    q = env.client.get("/v1/data-quality/events?kind=stale").json()
    assert q[0]["instrument"] == "NSE:MOCKA" and q[0]["severity"] == "warning"


def test_swing_freshness_uses_stored_daily_bars(env: Env) -> None:
    _sync(env)
    wid = _watchlist(env, horizon="SWING", n=2)
    env.client.post("/v1/marketdata/bars/NSE/MOCKA/backfill?interval=1d&days=10")
    res = env.client.get(f"/v1/marketdata/freshness?watchlist_id={wid}").json()
    by = {r["instrument"]: r["freshness"]["status"] for r in res}
    assert by == {"NSE:MOCKA": "FRESH", "NSE:MOCKB": "MISSING"}
    a = next(r for r in res if r["instrument"] == "NSE:MOCKA")
    assert a["is_mock"] is True and a["source"] == "db:MOCK"  # provenance of stored bars


def test_any_horizon_requires_explicit_choice(env: Env) -> None:
    _sync(env)
    wid = _watchlist(env, horizon="ANY", n=1)
    assert env.client.get(f"/v1/marketdata/freshness?watchlist_id={wid}").status_code == 422
    ok = env.client.get(f"/v1/marketdata/freshness?watchlist_id={wid}&horizon=INTRADAY")
    assert ok.status_code == 200


def test_no_provider_configured_fails_closed(db_url: str, migrated: Engine) -> None:
    app = create_app(make_settings(database_url=db_url), engine=migrated, configure_logs=False)
    with TestClient(app) as c:
        assert c.get("/v1/marketdata/quote/NSE/MOCKA").status_code == 503
        assert c.post("/v1/instruments/sync").status_code == 503
        wid = c.post("/v1/watchlists", json={"name": "empty", "horizon": "INTRADAY"}).json()["id"]
        assert c.get(f"/v1/marketdata/freshness?watchlist_id={wid}").json() == []
