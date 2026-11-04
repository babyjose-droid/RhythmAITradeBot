"""Read-only Sharekhan link end to end on PostgreSQL against a fake Sharekhan API.

Owner signs in (password + TOTP), re-authenticates, asks for a login URL, "Sharekhan"
redirects to the callback, reads work, verification stores shapes only, logout revokes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select, text

from rhythm.api.app import build_sharekhan
from rhythm.api.routes import auth as auth_routes
from rhythm.api.routes import broker as broker_routes
from rhythm.broker.sharekhan import token as tok
from rhythm.broker.sharekhan.adapter import StreamProbe
from rhythm.core.clock import to_exchange_local
from rhythm.db.models import BrokerObservationRow, BrokerSessionRow
from rhythm.db.session import session_scope
from tests import sharekhan_fake as fake
from tests.integration.test_phase8_api import KEY, PW, Env8

pytestmark = pytest.mark.integration


class EnvSK(Env8):
    def __init__(self, db_url: str, engine: Engine) -> None:
        super().__init__(db_url, engine)
        self.fake = fake.FakeSharekhan()
        st = self.state
        # Same app, but the read client talks to the fake API instead of the network.
        st.settings = st.settings.model_copy(
            update={
                "broker_adapter": "sharekhan",
                "sharekhan": st.settings.sharekhan.model_copy(
                    update={"api_key": _s(fake.API_KEY), "secret_key": _s(fake.SECRET)}
                ),
            }
        )
        st.sharekhan = build_sharekhan(st, transport=self.fake.transport())
        st.broker = st.sharekhan.broker

    def login_again(self) -> TestClient:
        """Sign in again after the clock jumped (the earlier session's idle timer lapsed)."""
        return self.login("owner")


def _s(v: str) -> SecretStr:
    return SecretStr(v)


@pytest.fixture
def envsk(db_url: str, migrated: Engine) -> Iterator[EnvSK]:
    auth_routes._limiter._hits.clear()  # process-wide sign-in limiter; other suites use it too
    e = EnvSK(db_url, migrated)
    with e.client():
        yield e


def _connect(e: EnvSK) -> tuple[TestClient, str]:
    e.add_user("owner", "owner")
    owner = e.login("owner")
    e.tick(6 * 60)  # sign-in counts as a re-authentication for 5 minutes
    r = owner.post("/v1/broker/login")
    assert r.status_code == 403 and r.json()["detail"] == "reauth_required"
    assert (
        owner.post(
            "/v1/auth/reauth", json={"password": PW["owner"], "code": e.code("owner")}
        ).status_code
        == 200
    )
    r = owner.post("/v1/broker/login")
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(r.json()["login_url"]).query)
    assert q["api_key"] == [fake.API_KEY] and "secret" not in r.text.lower()
    return owner, q["state"][0]


def test_connect_read_verify_logout(envsk: EnvSK) -> None:
    owner, state = _connect(envsk)
    anon = envsk.client()
    cb = anon.get(
        "/v1/broker/callback",
        params={"request_token": fake.request_token(), "state": state},
        follow_redirects=False,
    )
    assert cb.status_code == 302 and cb.headers["location"].endswith("?connected=1")
    # Single use: replaying the same redirect fails.
    again = anon.get(
        "/v1/broker/callback",
        params={"request_token": fake.request_token(), "state": state},
        follow_redirects=False,
    )
    assert "already+used" in again.headers["location"]

    st = owner.get("/v1/broker/status").json()
    assert st["connected"] and st["read_only"] and st["account_ref"] == "****567"
    assert st["order_placement"] == "not implemented"

    # Stored sealed: neither the access token nor the customer id appears in the table.
    with session_scope(envsk.state.session_factory) as s:
        dump = str(s.execute(text("SELECT * FROM broker_sessions")).all())
    assert fake.ACCESS not in dump and fake.CUSTOMER not in dump

    assert owner.get("/v1/broker/funds").json()["cash_balance"] == "125000.50"
    assert owner.get("/v1/broker/holdings").json()[0]["symbol"] == "TCS"
    assert [p["symbol"] for p in owner.get("/v1/broker/positions").json()] == ["RELIANCE"]
    assert owner.get("/v1/broker/orders").json()[0]["status"] == "FullyExecuted"
    assert owner.get("/v1/broker/profile").json()["origin"] == "SHAREKHAN"

    # Scrip master sync via the existing instruments route, then price history by symbol.
    assert owner.post("/v1/instruments/sync").status_code == 200
    h = owner.get("/v1/broker/historical/NSE/RELIANCE").json()
    assert [c["close"] for c in h["candles"]] == ["2400", "2415"]
    assert h["adjusted_for_corporate_actions"] is False

    v = owner.post("/v1/broker/verify", json={}).json()
    outcomes = {c["check"]: c["outcome"] for c in v["checks"]}
    assert outcomes == {
        "profile": "OK",
        "funds_nse": "OK",
        "holdings": "OK",
        "positions": "OK",
        "day_orders": "OK",
        "scrip_master_nse": "OK",
        "historical_daily": "OK",
    }
    with session_scope(envsk.state.session_factory) as s:
        shapes = str([o.shape for o in s.execute(select(BrokerObservationRow)).scalars()])
    for value in ("125000", "TCS", "RELIANCE", "3500", fake.CUSTOMER):
        assert value not in shapes

    # Only GETs and the one token POST ever reached "Sharekhan".
    assert {(r.method, r.url.path) for r in envsk.fake.requests if r.method != "GET"} == {
        ("POST", "/skapi/services/access/token")
    }

    assert owner.post("/v1/broker/logout").json()["revoked"] == 1
    assert owner.get("/v1/broker/holdings").status_code == 401
    with session_scope(envsk.state.session_factory) as s:
        assert s.execute(select(BrokerSessionRow.revoked_at)).scalar_one() is not None
        events = {r[0] for r in s.execute(text("SELECT event_type FROM audit_events"))}
    assert {"broker.session.created", "broker.verification.run", "broker.session.revoked"} <= events


def test_roles_bad_state_and_expiry(envsk: EnvSK) -> None:
    _, state = _connect(envsk)
    anon = envsk.client()
    bad = anon.get(
        "/v1/broker/callback",
        params={"request_token": fake.request_token(), "state": "forged"},
        follow_redirects=False,
    )
    assert "unknown+login+state" in bad.headers["location"]
    assert anon.get("/v1/broker/holdings").status_code == 401  # not signed in to Rhythm

    # Viewers cannot see account data; the login link expires after 10 minutes.
    envsk.add_user("view", "viewer")
    viewer = envsk.login("view")
    assert viewer.get("/v1/broker/holdings").status_code == 403
    assert viewer.get("/v1/broker/status").status_code == 200
    envsk.tick(11 * 60)
    late = anon.get(
        "/v1/broker/callback",
        params={"request_token": fake.request_token(), "state": state},
        follow_redirects=False,
    )
    assert "expired" in late.headers["location"]
    assert KEY  # (imported to keep the shared auth secret in one place)


def test_wrong_secure_key_is_reported(envsk: EnvSK) -> None:
    _, state = _connect(envsk)
    foreign = tok.encrypt(f"{fake.REQUEST_ID}|{fake.CUSTOMER}", "z" * 32)
    r = envsk.client().get(
        "/v1/broker/callback",
        params={"request_token": foreign, "state": state},
        follow_redirects=False,
    )
    assert "did+not+decrypt" in r.headers["location"]
    with session_scope(envsk.state.session_factory) as s:
        assert s.execute(select(BrokerSessionRow)).first() is None


def test_stream_check_and_readiness_gate(envsk: EnvSK, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, state = _connect(envsk)
    envsk.client().get(
        "/v1/broker/callback",
        params={"request_token": fake.request_token(), "state": state},
        follow_redirects=False,
    )
    seen: dict[str, object] = {}

    def fake_probe(url: str, access: str, api_key: str, keys: list[str], seconds: float):  # type: ignore[no-untyped-def]
        seen.update(url=url, access=access, keys=keys)
        return StreamProbe(True, 7, 140, {"kind": "object"}, None)

    monkeypatch.setattr(broker_routes, "probe_stream", fake_probe)
    gate = lambda: {  # noqa: E731
        g["id"]: g["status"] for g in owner.get("/v1/system/live-readiness").json()["gates"]
    }
    assert owner.post("/v1/broker/verify", json={"historical_symbol": None}).status_code == 200
    assert gate()["DATA-03"] == "NOT_MET"  # reads passed, but no stream check yet

    # Out of market hours the probe runs but does not count as evidence.
    r = owner.post("/v1/broker/verify/stream", json={"symbols": ["NC2885"], "seconds": 2})
    assert r.status_code == 200 and r.json()["market_open"] is False and r.json()["note"]
    assert seen["access"] == fake.ACCESS and seen["keys"] == ["NC2885"]
    assert gate()["DATA-03"] == "NOT_MET"

    # Next trading morning, 11:15 IST (the clock only moves forward: TOTP steps are single use).
    cal = envsk.state.calendar
    day = to_exchange_local(envsk.clock.now()).date() + timedelta(days=1)
    while not cal.is_trading_day(day):
        day += timedelta(days=1)
    envsk.clock.set(cal.session_open(day) + timedelta(hours=2))
    owner = envsk.login_again()
    assert (
        owner.post("/v1/broker/verify/stream", json={"symbols": ["NC2885"]}).json()["messages"] == 7
    )
    assert gate()["DATA-03"] == "MET"
    # The audit trail never holds the access token.
    with session_scope(envsk.state.session_factory) as s:
        dump = str(s.execute(text("SELECT payload FROM audit_events")).all())
    assert fake.ACCESS not in dump and fake.CUSTOMER not in dump
