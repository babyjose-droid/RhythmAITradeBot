"""Phase 9 on PostgreSQL: scheduler (slots, one leader, one run per slot, missed slots,
failure isolation, auto-disable), failure injection, health alarms, readiness heartbeat,
metrics. MOCK data, PAPER only."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from rhythm.api.app import create_app
from rhythm.audit import ActorType
from rhythm.broker.mock import MockBrokerAdapter
from rhythm.core.clock import EXCHANGE_TZ, FixedClock
from rhythm.db.models import AuditEvent, JobRunRow, ScheduledJobRow
from rhythm.db.session import create_db_engine, create_session_factory, session_scope
from rhythm.events.mock import MockEventProvider
from rhythm.fundamentals.mock import MockFundamentalsProvider
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.provider import MarketDataUnavailableError
from rhythm.scheduler import jobs as jobs_mod
from rhythm.scheduler.service import Scheduler
from tests.conftest import UNREACHABLE_DB_URL, auto_idempotency, make_settings
from tests.integration.test_phase6_api import CAL, LT, QUALITY, START, WEAK

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _release_schedulers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every Scheduler made in a test gives up its advisory lock afterwards, even on failure."""
    made: list[Scheduler] = []
    orig = Scheduler.__init__

    def init(self: Scheduler, *a: Any, **k: Any) -> None:
        orig(self, *a, **k)
        made.append(self)

    monkeypatch.setattr(Scheduler, "__init__", init)
    yield
    for s in made:
        s.release()


def at(d: Any, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=EXCHANGE_TZ)


class Env9:
    def __init__(self, db_url: str, engine: Engine, *, market: Any = None, **settings: Any) -> None:
        self.clock = FixedClock(at(START, 15, 40))
        self.market = market or MockMarketDataProvider(clock=self.clock, calendar=CAL)
        mock = (
            self.market
            if isinstance(self.market, MockMarketDataProvider)
            else MockMarketDataProvider(clock=self.clock, calendar=CAL)
        )
        self.app = create_app(
            make_settings(
                **(
                    dict(
                        database_url=db_url,
                        market_data_provider="mock",
                        broker_adapter="mock",
                        event_provider="mock",
                        fundamentals_provider="mock",
                        trading_mode="PAPER",
                        features={"paper_trading": True},
                    )
                    | settings
                )
            ),
            engine=engine,
            clock=self.clock,
            calendar=CAL,
            market_data=self.market,
            broker=MockBrokerAdapter(clock=self.clock),
            events=MockEventProvider(),
            fundamentals=MockFundamentalsProvider(mock.price_at),
            configure_logs=False,
        )
        self.state = self.app.state.rhythm
        self.c = TestClient(self.app)
        auto_idempotency(self.c)

    def seed(self) -> tuple[int, int]:
        c = self.c
        assert c.post("/v1/instruments/sync").status_code == 200
        wid = c.post("/v1/watchlists", json={"name": "lt", "horizon": "LONG_TERM"}).json()["id"]
        for s in (QUALITY, WEAK):
            c.post(f"/v1/watchlists/{wid}/items", json={"instrument": s})
        c.post("/v1/fundamentals/sync", json={"watchlist_id": wid})
        c.post("/v1/events/sync", json={"watchlist_id": wid})
        aid = c.post("/v1/paper/accounts", json={"name": "sched"}).json()["id"]
        return wid, aid

    def job(self, **body: Any) -> dict[str, Any]:
        r = self.c.post("/v1/scheduler/jobs", json=body)
        assert r.status_code == 201, r.text
        out: dict[str, Any] = r.json()
        return out

    def enable(self, job_id: int) -> dict[str, Any]:
        r = self.c.post(f"/v1/scheduler/jobs/{job_id}/enable", json={"reason": "test"})
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        return out

    def runs(self, job_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.c.get(f"/v1/scheduler/jobs/{job_id}/runs").json()
        return out


@pytest.fixture
def env9(db_url: str, migrated: Engine) -> Iterator[Env9]:
    e = Env9(db_url, migrated)
    with e.c:
        yield e


def _lt_job(e: Env9, wid: int, aid: int) -> dict[str, Any]:
    params = {"account_id": aid, "watchlist_id": wid, "strategy": LT}
    return e.job(
        name="long-term after close", kind="paper_cycle", params=params, schedule="after_close"
    )


def test_job_validation_and_disabled_by_default(env9: Env9) -> None:
    wid, aid = env9.seed()
    bad = {"account_id": aid, "watchlist_id": wid, "strategy": LT}
    r = env9.c.post(
        "/v1/scheduler/jobs",
        json={"name": "wrong", "kind": "paper_cycle", "params": bad, "schedule": "intraday_5m"},
    )
    assert r.status_code == 422 and "after_close" in r.json()["detail"]
    r = env9.c.post(
        "/v1/scheduler/jobs",
        json={
            "name": "nowl",
            "kind": "events_sync",
            "params": {"watchlist_id": 999},
            "schedule": "after_close",
        },
    )
    assert r.status_code == 422 and "not found" in r.json()["detail"]
    j = _lt_job(env9, wid, aid)
    assert j["enabled"] is False and j["next_run_at"] is None
    assert (
        env9.c.post(
            "/v1/scheduler/jobs", json={**{k: j[k] for k in ("name", "kind", "params", "schedule")}}
        ).status_code
        == 409
    )


def test_scheduled_slot_runs_once_and_creates_paper_orders(env9: Env9) -> None:
    wid, aid = env9.seed()
    j = _lt_job(env9, wid, aid)
    en = env9.enable(j["id"])
    assert datetime.fromisoformat(en["next_run_at"]) == at(START, 15, 45)
    s = Scheduler(env9.state, instance="t1")
    assert s.tick()["runs"] == []  # 15:40: not due
    env9.clock.set(at(START, 15, 46))
    res = s.tick()
    (run,) = res["runs"]
    assert run["status"] == "SUCCESS" and run["detail"]["new_orders"] == 1, run
    assert s.tick()["runs"] == []  # same slot never runs twice
    (r,) = env9.runs(j["id"])
    assert r["status"] == "SUCCESS" and r["trigger"] == "schedule" and r["instance"] == "t1"
    (o,) = env9.c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert o["instrument"] == QUALITY
    cyc = env9.c.get("/v1/audit/events?event_type=paper.cycle").json()
    assert cyc[0]["actor_id"] == "scheduler" and cyc[0]["actor_type"] == ActorType.SYSTEM.value
    job = env9.c.get("/v1/scheduler/jobs").json()[0]
    assert job["last_status"] == "SUCCESS" and job["next_run_at"] > en["next_run_at"]
    s.release()


def test_one_leader_and_one_run_per_slot(env9: Env9) -> None:
    wid, aid = env9.seed()
    j = _lt_job(env9, wid, aid)
    env9.enable(j["id"])
    a, b = Scheduler(env9.state, instance="a"), Scheduler(env9.state, instance="b")
    env9.clock.set(at(START, 15, 46))
    assert a.tick()["leader"] is True
    assert b.tick() == {"leader": False, "at": b.tick()["at"], "runs": []}
    assert b.execute(j["id"], at(START, 15, 45), "schedule")["status"] == "DUPLICATE_SLOT"
    a.release()  # leader goes away: the standby takes over on its next tick
    assert b.tick()["leader"] is True
    beats = env9.c.get("/v1/scheduler/status").json()["instances"]
    assert {x["instance"] for x in beats} == {"a", "b"}
    b.release()


def test_missed_slots_are_recorded_not_run_late(env9: Env9) -> None:
    wid, aid = env9.seed()
    j = _lt_job(env9, wid, aid)
    env9.enable(j["id"])
    env9.clock.set(at(START, 15, 40) + timedelta(days=4))  # scheduler was down for days
    s = Scheduler(env9.state, instance="late")
    (m,) = s.tick()["runs"]
    assert m["status"] == "MISSED" and m["missed_slots"] >= 2
    assert env9.c.get(f"/v1/paper/accounts/{aid}/orders").json() == []
    (r,) = env9.runs(j["id"])
    assert r["status"] == "MISSED" and "not run late" in r["detail"]["reason"]
    s.release()


def test_failure_rolls_back_and_auto_disables(env9: Env9, monkeypatch: pytest.MonkeyPatch) -> None:
    env9.seed()
    j = env9.job(
        name="health",
        kind="health_check",
        schedule="every_minutes",
        schedule_arg="5",
        trading_days_only=False,
    )
    env9.enable(j["id"])

    def boom(session: Any, state: Any, kind: str, params: Any, now: Any) -> dict[str, Any]:
        state.audit.append(
            session, event_type="test.partial_write", actor_type=ActorType.SYSTEM, actor_id="t"
        )
        raise RuntimeError("injected failure token=abc123secret")

    monkeypatch.setattr(jobs_mod, "run", boom)
    s = Scheduler(env9.state, instance="f")
    for i in range(3):
        env9.clock.set(at(START, 15, 40) + timedelta(minutes=5 * (i + 1), seconds=5))
        (r,) = s.tick()["runs"]
        assert r["status"] == "FAILED"
    assert "abc123secret" not in r["detail"]["error"]  # errors are redacted
    with session_scope(env9.state.session_factory) as ses:
        assert (
            ses.execute(
                select(AuditEvent).where(AuditEvent.event_type == "test.partial_write")
            ).first()
            is None
        )
        job = ses.get(ScheduledJobRow, j["id"])
        assert job is not None and job.enabled is False
        assert (job.disabled_reason or "").startswith("auto")
    kinds = [n["kind"] for n in env9.c.get("/v1/notifications").json()]
    assert kinds.count("job_failed") == 2 and kinds.count("job_auto_disabled") == 1
    monkeypatch.undo()
    hc = Scheduler(env9.state, instance="h")
    j2 = env9.job(
        name="health2",
        kind="health_check",
        schedule="every_minutes",
        schedule_arg="5",
        trading_days_only=False,
    )
    out = hc.run_now(j2["id"], "tester")
    assert out["status"] == "SUCCESS" and out["detail"]["components"]["jobs"] == "down"
    s.release()
    hc.release()


def test_market_data_outage_skips_without_orders(db_url: str, migrated: Engine) -> None:
    class Flaky(MockMarketDataProvider):
        down = False

        def bars(self, *a: Any, **k: Any) -> Any:
            if self.down:
                raise MarketDataUnavailableError("injected outage")
            return super().bars(*a, **k)

    clock = FixedClock(at(START, 15, 40))
    flaky = Flaky(clock=clock, calendar=CAL)
    e = Env9(db_url, migrated, market=flaky)
    e.clock = clock
    e.state.clock = clock
    with e.c:
        wid, aid = e.seed()
        j = _lt_job(e, wid, aid)
        flaky.down = True
        out = Scheduler(e.state, instance="o").run_now(j["id"], "tester")
        assert out["status"] == "SUCCESS" and out["detail"]["new_orders"] == 0
        assert all("unavailable" in v for v in out["detail"]["skipped"].values())
        assert e.c.get(f"/v1/paper/accounts/{aid}/orders").json() == []


def test_database_outage_does_not_kill_the_scheduler(env9: Env9) -> None:
    bad = create_db_engine(UNREACHABLE_DB_URL)
    broken = dataclasses.replace(
        env9.state, engine=bad, session_factory=create_session_factory(bad)
    )
    s = Scheduler(broken, instance="dbdown")
    with pytest.raises(Exception, match=r"connect|Operational|refused"):
        s.tick()  # the loop catches and logs this; the process keeps running
    assert s.is_leader is False
    ok = Scheduler(env9.state, instance="dbup")
    assert ok.tick()["leader"] is True  # recovers as soon as the database is back
    ok.release()


def test_readiness_needs_scheduler_heartbeat(db_url: str, migrated: Engine) -> None:
    e = Env9(db_url, migrated, scheduler={"expected": True, "heartbeat_stale_seconds": 120})

    def sched_check() -> tuple[int, dict[str, Any]]:
        r = e.c.get("/health/ready")
        (chk,) = [x for x in r.json()["checks"] if x["name"] == "scheduler"]
        return r.status_code, chk

    with e.c:
        code, chk = sched_check()
        assert code == 503 and chk["ok"] is False and "no leader" in chk["detail"]["error"]
        s = Scheduler(e.state, instance="hb")
        s.tick()
        assert sched_check()[1]["ok"] is True
        e.clock.set(e.clock.now() + timedelta(minutes=5))
        code, chk = sched_check()
        assert code == 503 and chk["ok"] is False  # stale heartbeat fails closed


def test_health_alarms_on_transitions_only(env9: Env9) -> None:
    env9.seed()
    j = env9.job(
        name="health",
        kind="health_check",
        schedule="every_minutes",
        schedule_arg="5",
        trading_days_only=False,
    )
    s = Scheduler(env9.state, instance="hc")
    assert s.run_now(j["id"], "t")["detail"]["changed"] == []
    env9.c.post("/v1/risk/kill-switch", json={"action": "activate", "reason": "drill"})
    env9.clock.set(env9.clock.now() + timedelta(minutes=1))
    assert s.run_now(j["id"], "t")["detail"]["changed"] == [
        {"component": "kill_switch", "from": None, "to": "degraded"}
    ]
    env9.clock.set(env9.clock.now() + timedelta(minutes=1))
    assert s.run_now(j["id"], "t")["detail"]["changed"] == []  # steady: no repeat alarm
    env9.c.post("/v1/risk/kill-switch", json={"action": "release", "reason": "done"})
    env9.clock.set(env9.clock.now() + timedelta(minutes=1))
    assert s.run_now(j["id"], "t")["detail"]["changed"][0]["to"] == "ok"
    titles = [
        n["title"]
        for n in env9.c.get("/v1/notifications").json()
        if n["kind"] == "health_kill_switch"
    ]
    assert titles == ["kill switch recovered", "kill switch is DEGRADED"]
    s.release()


def test_sync_and_audit_jobs(env9: Env9) -> None:
    wid, _ = env9.seed()
    s = Scheduler(env9.state, instance="sy")
    for kind in ("events_sync", "fundamentals_sync"):
        j = env9.job(name=kind, kind=kind, params={"watchlist_id": wid}, schedule="after_close")
        assert s.run_now(j["id"], "t")["status"] == "SUCCESS"
    a = env9.job(name="audit", kind="audit_verify", schedule="daily_at", schedule_arg="18:00")
    out = s.run_now(a["id"], "t")
    assert out["status"] == "SUCCESS" and out["detail"]["ok"] is True
    assert env9.runs(a["id"])[0]["trigger"] == "manual"
    s.release()


def test_run_now_endpoint_and_metrics(db_url: str, migrated: Engine) -> None:
    e = Env9(db_url, migrated, metrics={"token": "metrics-test-token-123"})
    with e.c as c:
        j = e.job(name="audit", kind="audit_verify", schedule="daily_at", schedule_arg="18:00")
        r = c.post(f"/v1/scheduler/jobs/{j['id']}/run")
        assert r.status_code == 200 and r.json()["status"] == "SUCCESS"
        assert c.get("/metrics").status_code == 401
        m = c.get("/metrics", headers={"Authorization": "Bearer metrics-test-token-123"})
        assert m.status_code == 200 and m.headers["content-type"].startswith("text/plain")
        body = m.text
        for metric in (
            "rhythm_up{",
            "rhythm_kill_switch_active 0",
            "rhythm_approvals_pending 0",
            'rhythm_job_consecutive_failures{enabled="false",job="audit"} 0',
            "rhythm_scheduler_heartbeat_age_seconds -1",
            'rhythm_http_requests_total{method="POST",'
            'route="/v1/scheduler/jobs/{job_id}/run",status="200"} 1',
        ):
            assert metric in body, metric
        ov = c.get("/v1/automation").json()["scheduler"]
        assert ov["configured"] is True and ov["jobs"] == 1 and ov["enabled_jobs"] == 0


def test_job_runs_are_unique_per_slot_in_the_database(env9: Env9, migrated: Engine) -> None:
    j = env9.job(name="audit", kind="audit_verify", schedule="daily_at", schedule_arg="18:00")
    slot = at(START, 18)
    with migrated.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO job_runs (job_id, slot, trigger, started_at, status, instance, "
                "detail) "
                "VALUES (:j, :s, 'schedule', now(), 'SUCCESS', 'x', '{}')"
            ),
            {"j": j["id"], "s": slot},
        )
    with pytest.raises(Exception, match="uq_job_runs_slot"), migrated.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO job_runs (job_id, slot, trigger, started_at, status, instance, "
                "detail) "
                "VALUES (:j, :s, 'schedule', now(), 'SUCCESS', 'y', '{}')"
            ),
            {"j": j["id"], "s": slot},
        )
    with session_scope(env9.state.session_factory) as s:
        assert len(s.execute(select(JobRunRow)).all()) == 1
