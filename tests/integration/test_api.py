from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from rhythm.api.app import create_app
from rhythm.core.clock import FixedClock
from rhythm.db import migrate
from rhythm.db.models import SystemHealthEvent
from rhythm.db.session import create_session_factory, session_scope
from tests.conftest import make_settings

pytestmark = pytest.mark.integration


def _client(db_url: str, engine: Engine, **kw: object) -> TestClient:
    app = create_app(make_settings(database_url=db_url), engine=engine, configure_logs=False, **kw)  # type: ignore[arg-type]
    return TestClient(app)


def test_ready_when_db_migrated(db_url: str, migrated: Engine) -> None:
    with _client(db_url, migrated) as c:
        r = c.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready"
    assert all(ch["ok"] for ch in r.json()["checks"])


def test_lifecycle_events_audited_and_chain_valid(db_url: str, migrated: Engine) -> None:
    with _client(db_url, migrated) as c:
        events = c.get("/v1/audit/events").json()
        assert [e["event_type"] for e in events] == ["system.started"]
        assert events[0]["payload"]["trading_mode"] == "RESEARCH_ONLY"
    # after shutdown, a stop event exists and the chain verifies
    with _client(db_url, migrated) as c:
        types = [e["event_type"] for e in c.get("/v1/audit/events?limit=10").json()]
        assert types == ["system.started", "system.stopped", "system.started"]
        v = c.get("/v1/audit/verify").json()
    assert v == {"ok": True, "rows_checked": 3, "first_bad_id": None, "reason": None}


def test_audit_pagination(db_url: str, migrated: Engine) -> None:
    for _ in range(3):
        with _client(db_url, migrated):
            pass
    with _client(db_url, migrated) as c:
        first = c.get("/v1/audit/events?limit=2").json()
        older = c.get(f"/v1/audit/events?limit=50&before_id={first[-1]['id']}").json()
    assert len(first) == 2
    assert all(e["id"] < first[-1]["id"] for e in older)
    assert c.get("/v1/audit/events?limit=0").status_code == 422


def test_not_ready_when_migrations_missing(db_url: str, migrated: Engine) -> None:
    migrate.downgrade(db_url, "base")
    migrated.dispose()
    with _client(db_url, migrated) as c:
        r = c.get("/health/ready")
    assert r.status_code == 503
    checks = {ch["name"]: ch for ch in r.json()["checks"]}
    assert checks["database"]["ok"] is True
    assert checks["migrations"]["ok"] is False
    assert checks["migrations"]["detail"]["current"] is None


def test_not_ready_on_clock_skew_and_health_event_recorded(db_url: str, migrated: Engine) -> None:
    skewed = FixedClock(datetime(2020, 1, 1, tzinfo=UTC))
    with _client(db_url, migrated, clock=skewed) as c:
        r = c.get("/health/ready")
        c.get("/health/ready")  # unchanged state: no duplicate health event
    assert r.status_code == 503
    checks = {ch["name"]: ch["ok"] for ch in r.json()["checks"]}
    assert checks == {
        "database": True,
        "clock_skew": False,
        "migrations": True,
        "trading_mode": True,
    }
    with session_scope(create_session_factory(migrated)) as s:
        events = s.execute(select(SystemHealthEvent)).scalars().all()
    assert [(e.component, e.status) for e in events] == [("api.readiness", "down")]
