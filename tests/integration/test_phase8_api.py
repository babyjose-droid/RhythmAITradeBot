"""Phase 8 end to end on PostgreSQL: sign-in with password + TOTP, sessions, CSRF, roles,
re-authentication, lockout; approvals queue; notifications (+ webhook outbox); automation
overview; audit filters. MOCK data, PAPER only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from rhythm.api.app import create_app
from rhythm.auth import totp
from rhythm.broker.mock import MockBrokerAdapter
from rhythm.cli import main
from rhythm.config import get_settings
from rhythm.config.settings import NotificationSettings
from rhythm.core.clock import FixedClock
from rhythm.db.session import session_scope
from rhythm.events.mock import MockEventProvider
from rhythm.fundamentals.mock import MockFundamentalsProvider
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.notifications import service as notes
from tests.conftest import auto_idempotency, make_settings
from tests.integration.test_phase6_api import CAL, LT, QUALITY, START, WEAK, after_close

pytestmark = pytest.mark.integration

KEY = "integration-test-secret-" + "Zq7" * 10
PW = {"owner": "Tq9-first-key!x", "appr": "Tq9-second-key!y", "view": "Tq9-third-key!zz"}


class Env8:
    def __init__(self, db_url: str, engine: Engine, *, auth: bool = True) -> None:
        self.clock = FixedClock(after_close(START))
        market = MockMarketDataProvider(clock=self.clock, calendar=CAL)
        self.app = create_app(
            make_settings(
                database_url=db_url,
                market_data_provider="mock",
                broker_adapter="mock",
                event_provider="mock",
                fundamentals_provider="mock",
                trading_mode="PAPER",
                features={"paper_trading": True},
                auth={"enabled": auth, "secret_key": KEY, "cookie_secure": False},
            ),
            engine=engine,
            clock=self.clock,
            calendar=CAL,
            market_data=market,
            broker=MockBrokerAdapter(clock=self.clock),
            events=MockEventProvider(),
            fundamentals=MockFundamentalsProvider(market.price_at),
            configure_logs=False,
        )
        self.state = self.app.state.rhythm
        self.secrets: dict[str, str] = {}

    def client(self) -> TestClient:
        c = TestClient(self.app)
        auto_idempotency(c)
        return c

    def add_user(self, name: str, role: str) -> None:
        with session_scope(self.state.session_factory) as s:
            e = self.state.auth.create_user(
                s,
                username=name,
                password=PW[name],
                role=role,
                created_by="test",
                at=self.clock.now(),
            )
        self.secrets[name] = e.totp_secret

    def code(self, name: str) -> str:
        return totp.code_at(self.secrets[name], totp.step_of(self.clock.now()))

    def tick(self, seconds: int = 31) -> None:
        self.clock.set(self.clock.now() + timedelta(seconds=seconds))

    def login(self, name: str) -> TestClient:
        c = self.client()
        r = c.post(
            "/v1/auth/login", json={"username": name, "password": PW[name], "code": self.code(name)}
        )
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        self.tick()  # the next code is a new step (codes are single-use)
        return c


@pytest.fixture
def env8(db_url: str, migrated: Engine) -> Iterator[Env8]:
    e = Env8(db_url, migrated)
    with e.client():
        yield e


def test_login_session_csrf_and_logout(env8: Env8) -> None:
    anon = env8.client()
    st = anon.get("/v1/auth/status").json()
    assert st == {"enabled": True, "has_users": False, "setup_hint": st["setup_hint"]}
    assert "user-create" in st["setup_hint"]
    assert anon.get("/health/live").status_code == 200  # public
    assert anon.get("/v1/watchlists").status_code == 401
    env8.add_user("owner", "owner")
    bad = anon.post(
        "/v1/auth/login", json={"username": "owner", "password": PW["owner"], "code": "000000"}
    )
    assert bad.status_code == 401 and bad.json()["detail"] == "invalid credentials"
    nouser = anon.post(
        "/v1/auth/login", json={"username": "ghost", "password": "x" * 12, "code": "123456"}
    )
    assert nouser.status_code == 401 and nouser.json()["detail"] == "invalid credentials"
    c = env8.login("owner")
    r = c.post("/v1/auth/login", json={"username": "owner", "password": PW["owner"], "code": "1"})
    assert r.status_code == 422
    me = c.get("/v1/auth/me").json()
    assert me["username"] == "owner" and me["role"] == "owner"
    sc = c.cookies.get("rhythm_session")
    assert sc and len(sc) > 30
    # mutating without the CSRF header is refused even with a valid session cookie
    no_csrf = env8.client()
    no_csrf.cookies.set("rhythm_session", sc)
    r = no_csrf.post("/v1/watchlists", json={"name": "x", "horizon": "SWING"})
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    assert c.post("/v1/watchlists", json={"name": "x", "horizon": "SWING"}).status_code == 201
    ev = c.get("/v1/audit/events?event_type=watchlist.*").json()
    assert ev and ev[0]["actor_id"] == "owner"  # actions are attributed to the signed-in user
    fails = c.get("/v1/audit/events?event_type=auth.login_failed").json()
    assert len(fails) == 2 and {f["actor_id"] for f in fails} == {"owner", "ghost"}
    assert c.post("/v1/auth/logout").json() == {"logged_out": True}
    again = env8.client()
    again.cookies.set("rhythm_session", sc)
    assert again.get("/v1/auth/me").status_code == 401  # revoked server-side
    assert c.get("/v1/audit/verify").status_code == 401


def test_code_replay_lockout_and_idle_timeout(env8: Env8) -> None:
    env8.add_user("owner", "owner")
    anon = env8.client()
    code = env8.code("owner")
    body = {"username": "owner", "password": PW["owner"], "code": code}
    assert anon.post("/v1/auth/login", json=body).status_code == 200
    assert anon.post("/v1/auth/login", json=body).status_code == 401  # same code: replay
    env8.tick()
    for _ in range(5):
        anon.post("/v1/auth/login", json=body | {"password": "wrong-password-1"})
    good = body | {"code": env8.code("owner")}
    assert anon.post("/v1/auth/login", json=good).status_code == 401  # locked
    env8.tick(16 * 60)
    c = env8.login("owner")  # lock expired
    env8.tick(31 * 60)  # idle longer than session_idle_minutes
    assert c.get("/v1/auth/me").status_code == 401


def test_roles_and_reauthentication(env8: Env8) -> None:
    for n, r in (("owner", "owner"), ("appr", "approver"), ("view", "viewer")):
        env8.add_user(n, r)
    viewer, approver, owner = env8.login("view"), env8.login("appr"), env8.login("owner")
    assert viewer.get("/v1/risk/controls").status_code == 200
    r = viewer.post("/v1/watchlists", json={"name": "v", "horizon": "SWING"})
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    assert viewer.get("/v1/users").status_code == 403
    ks = {"action": "activate", "reason": "drill by approver"}
    assert approver.post("/v1/risk/kill-switch", json=ks).status_code == 200
    rel = {"action": "release", "reason": "drill over"}
    assert approver.post("/v1/risk/kill-switch", json=rel).status_code == 403  # owner only
    env8.tick(6 * 60)  # owner's login-time re-auth is now stale
    r = owner.post("/v1/risk/kill-switch", json=rel)
    assert r.status_code == 403 and r.json()["detail"] == "reauth_required"
    wrong = owner.post("/v1/auth/reauth", json={"password": PW["owner"], "code": "000000"})
    assert wrong.status_code == 401
    ok = owner.post("/v1/auth/reauth", json={"password": PW["owner"], "code": env8.code("owner")})
    assert ok.status_code == 200
    assert owner.post("/v1/risk/kill-switch", json=rel).json()["kill_switch_active"] is False
    # user admin is owner + re-auth; the new user's secret is shown once
    new = owner.post(
        "/v1/users", json={"username": "bob", "role": "viewer", "password": "Kw8-fresh-key!x9"}
    )
    assert new.status_code == 201, new.text
    assert new.json()["provisioning_uri"].startswith("otpauth://")
    assert "totp_secret" not in json.dumps(owner.get("/v1/users").json())
    assert owner.post("/v1/users/owner/disable").status_code == 409  # last owner
    weak = owner.post("/v1/users", json={"username": "eve", "role": "viewer", "password": "short"})
    assert weak.status_code == 422


def test_password_change_revokes_other_sessions(env8: Env8) -> None:
    env8.add_user("owner", "owner")
    a, b = env8.login("owner"), env8.login("owner")
    new_pw = "brand-new-pass-Zq7"
    r = a.post(
        "/v1/auth/password",
        json={"current_password": PW["owner"], "new_password": new_pw, "code": env8.code("owner")},
    )
    assert r.status_code == 200
    assert a.get("/v1/auth/me").status_code == 200 and b.get("/v1/auth/me").status_code == 401


# ---------------------------------------------------------------------------- approvals
def _setup(c: TestClient) -> tuple[int, int]:
    assert c.post("/v1/instruments/sync").status_code == 200
    wid = c.post("/v1/watchlists", json={"name": "lt", "horizon": "LONG_TERM"}).json()["id"]
    for s in (QUALITY, WEAK):
        c.post(f"/v1/watchlists/{wid}/items", json={"instrument": s})
    c.post("/v1/fundamentals/sync", json={"watchlist_id": wid})
    c.post("/v1/events/sync", json={"watchlist_id": wid})
    aid = c.post("/v1/paper/accounts", json={"name": "appr"}).json()["id"]
    pol = c.post(
        "/v1/risk/policies",
        json={
            "params": {"review_above_order_value": "1000"},
            "reason": "test: everything needs review",
        },
    )
    assert pol.status_code == 201, pol.text
    return wid, aid


def _cycle(c: TestClient, aid: int, wid: int) -> dict[str, Any]:
    r = c.post(f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid, "strategy": LT})
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


@pytest.fixture
def noauth(db_url: str, migrated: Engine) -> Iterator[tuple[Env8, TestClient]]:
    e = Env8(db_url, migrated, auth=False)
    with e.client() as c:
        yield e, c


def test_review_candidate_is_queued_then_approved(noauth: tuple[Env8, TestClient]) -> None:
    _, c = noauth
    wid, aid = _setup(c)
    r = _cycle(c, aid, wid)
    assert r["new_orders"] == 0
    (s,) = [x for x in r["signals"] if x["type"] == "ENTER_LONG"]
    assert s["decision"] == "REQUIRES_REVIEW" and s["approval_id"]
    _cycle(c, aid, wid)  # same idea again: not queued twice
    (a,) = c.get("/v1/approvals?status=PENDING").json()
    assert a["source"] == "RISK" and a["instrument"] == QUALITY and a["detail"]["thesis"]
    (n,) = [x for x in c.get("/v1/notifications").json() if x["kind"] == "approval_pending"]
    assert n["severity"] == "warning" and n["link"] == "/approvals"
    assert c.get("/v1/notifications/summary").json()["unread"]["warning"] >= 1
    d = c.post(
        f"/v1/approvals/{a['id']}/decision",
        json={"decision": "approve", "reason": "checked the thesis"},
    )
    assert d.status_code == 200, d.text
    out = d.json()
    assert out["status"] == "APPROVED", out["decision_reason"]
    assert out["order_id"] and out["decided_by"] == "local-owner"
    assert out["decision_risk"]["outcome"] in ("ACCEPT", "REDUCE")  # risk re-ran at approval
    (o,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert o["id"] == out["order_id"] and o["instrument"] == QUALITY
    (th,) = c.get(f"/v1/theses?account_id={aid}").json()
    assert th["status"] == "PENDING_ENTRY"
    again = c.post(
        f"/v1/approvals/{a['id']}/decision", json={"decision": "approve", "reason": "twice"}
    )
    assert again.status_code == 409
    types = {e["event_type"] for e in c.get("/v1/audit/events?event_type=approval.*").json()}
    assert {"approval.requested", "approval.approved", "approval.executed"} <= types
    assert c.get("/v1/audit/verify").json()["ok"] is True


def test_approval_reject_expire_and_failed_recheck(noauth: tuple[Env8, TestClient]) -> None:
    e, c = noauth
    wid, aid = _setup(c)
    _cycle(c, aid, wid)
    (a,) = c.get("/v1/approvals?status=PENDING").json()
    c.post("/v1/risk/kill-switch", json={"action": "activate", "reason": "test block"})
    f = c.post(
        f"/v1/approvals/{a['id']}/decision", json={"decision": "approve", "reason": "try anyway"}
    )
    assert f.json()["status"] == "FAILED" and "R2" in f.json()["decision_reason"]
    assert c.get(f"/v1/paper/accounts/{aid}/orders").json() == []
    crit = [n for n in c.get("/v1/notifications").json() if n["kind"] == "kill_switch_activated"]
    assert crit and crit[0]["severity"] == "critical"
    c.post("/v1/risk/kill-switch", json={"action": "release", "reason": "done"})
    e.clock.set(e.clock.now() + timedelta(days=1))
    _cycle(c, aid, wid)
    (b,) = c.get("/v1/approvals?status=PENDING").json()
    rj = c.post(
        f"/v1/approvals/{b['id']}/decision", json={"decision": "reject", "reason": "not now"}
    )
    assert rj.json()["status"] == "REJECTED" and rj.json()["order_id"] is None
    e.clock.set(e.clock.now() + timedelta(days=1))
    _cycle(c, aid, wid)
    (p,) = c.get("/v1/approvals?status=PENDING").json()
    e.clock.set(e.clock.now() + timedelta(days=5))
    assert c.get(f"/v1/approvals/{p['id']}").json()["status"] == "EXPIRED"
    assert c.post("/v1/notifications/read", json={"ids": None}).json()["marked_read"] >= 3
    assert c.get("/v1/notifications/summary").json()["unread_total"] == 0


def test_approver_can_decide_viewer_cannot(env8: Env8) -> None:
    env8.add_user("owner", "owner")
    env8.add_user("appr", "approver")
    env8.add_user("view", "viewer")
    owner = env8.login("owner")
    wid, aid = _setup(owner)
    _cycle(owner, aid, wid)
    (a,) = owner.get("/v1/approvals?status=PENDING").json()
    body = {"decision": "approve", "reason": "approver check"}
    assert (
        env8.login("view").post(f"/v1/approvals/{a['id']}/decision", json=body).status_code == 403
    )
    r = env8.login("appr").post(f"/v1/approvals/{a['id']}/decision", json=body)
    assert r.json()["status"] == "APPROVED" and r.json()["decided_by"] == "appr"


# ---------------------------------------------------------------------------- notifications
def test_webhook_outbox_delivery_and_backoff(
    noauth: tuple[Env8, TestClient], migrated: Engine
) -> None:
    e, _ = noauth
    cfg = NotificationSettings(webhook_url="https://hooks.example.test/T000/secret", max_attempts=2)
    seen: list[dict[str, Any]] = []
    fail = {"on": False}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(500 if fail["on"] else 200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    now = e.clock.now()
    with session_scope(e.state.session_factory) as s:
        n1 = notes.notify(s, kind="t", severity="critical", title="hello", at=now, settings=cfg)
        n2 = notes.notify(s, kind="t", severity="info", title="quiet", at=now, settings=cfg)
        assert (n1.webhook_status, n2.webhook_status) == ("PENDING", "SKIPPED")
        assert notes.deliver_pending(s, cfg, now=now, environment="test", client=client) == {
            "sent": 1,
            "failed": 0,
        }
        assert seen[0]["severity"] == "critical" and "hello" in seen[0]["text"]
        assert "hooks.example" not in json.dumps(seen[0])
        fail["on"] = True
        n3 = notes.notify(s, kind="t", severity="warning", title="retry me", at=now, settings=cfg)
        notes.deliver_pending(s, cfg, now=now, environment="test", client=client)
        assert n3.webhook_status == "PENDING"
        assert n3.webhook_next_at is not None and n3.webhook_next_at > now
        notes.deliver_pending(
            s, cfg, now=now + timedelta(minutes=5), environment="test", client=client
        )
        assert n3.webhook_status == "FAILED" and n3.webhook_error == "HTTP 500"


def test_automation_overview_and_audit_filters(noauth: tuple[Env8, TestClient]) -> None:
    _, c = noauth
    wid, aid = _setup(c)
    _cycle(c, aid, wid)
    ov = c.get("/v1/automation").json()
    lt = next(s for s in ov["strategies"] if s["name"] == LT)
    assert lt["last_run"]["account_id"] == str(aid) and lt["horizon"] == "LONG_TERM"
    assert ov["approvals"]["pending"] == 1 and ov["scheduler"]["configured"] is False
    assert ov["notifications"]["webhook_configured"] is False
    cyc = c.get(
        f"/v1/audit/events?event_type=paper.cycle&subject_type=paper_account&subject_id={aid}"
    ).json()
    assert len(cyc) == 1 and cyc[0]["payload"]["strategy"].startswith(LT)
    assert c.get("/v1/audit/events?actor_id=nobody").json() == []


def test_cli_user_create_prints_secret_once(
    db_url: str,
    migrated: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:

    for k, v in {
        "RHYTHM_DATABASE_URL": db_url,
        "RHYTHM_ENVIRONMENT": "test",
        "RHYTHM_AUTH__SECRET_KEY": KEY,
        "RHYTHM_NEW_USER_PASSWORD": "Cli-made-key!77",
        "RHYTHM_LOG_LEVEL": "ERROR",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        assert main(["user-create", "--username", "owner", "--role", "owner"]) == 0
        out = capsys.readouterr().out
        assert "otpauth://totp/" in out and "shown once" in out
        assert main(["user-create", "--username", "owner", "--role", "owner"]) == 2  # exists
        monkeypatch.setenv("RHYTHM_NEW_USER_PASSWORD", "short")
        assert main(["user-create", "--username", "second", "--role", "viewer"]) == 2
    finally:
        get_settings.cache_clear()
