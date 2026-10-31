from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from rhythm.audit import ActorType, AuditLog
from rhythm.audit.service import GENESIS_HASH
from rhythm.core.clock import FixedClock
from rhythm.core.errors import AuditIntegrityError
from rhythm.db.models import AuditEvent
from rhythm.db.session import session_scope

pytestmark = pytest.mark.integration

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlLXVzZXIifQ.ZmFrZXNpZ25hdHVyZQ"


def _append(sf: sessionmaker[Session], log: AuditLog, n: int = 1, **kw: object) -> None:
    for i in range(n):
        with session_scope(sf) as s:
            log.append(
                s,
                event_type="test.event",
                actor_type=ActorType.SYSTEM,
                actor_id="tester",
                payload={"i": i, **kw},
            )


def test_chain_links_and_verifies(session_factory: sessionmaker[Session]) -> None:
    clock = FixedClock(datetime(2026, 10, 31, 4, 0, tzinfo=UTC))
    log = AuditLog(environment="test", clock=clock)
    for _ in range(3):
        _append(session_factory, log)
        clock.advance(timedelta(microseconds=1))
    with session_scope(session_factory) as s:
        rows = s.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars().all()
        assert rows[0].prev_hash == GENESIS_HASH
        assert rows[1].prev_hash == rows[0].row_hash
        assert rows[2].prev_hash == rows[1].row_hash
        assert rows[0].occurred_at == datetime(2026, 10, 31, 4, 0, tzinfo=UTC)
        res = AuditLog.verify(s)
    assert res.ok and res.rows_checked == 3


def test_payload_is_redacted_before_storage(session_factory: sessionmaker[Session]) -> None:
    log = AuditLog(environment="test")
    _append(
        session_factory, log, access_token=FAKE_JWT, customerId="12345678", note=f"t={FAKE_JWT}"
    )
    with session_scope(session_factory) as s:
        raw = s.execute(text("SELECT payload::text FROM audit_events")).scalar_one()
    assert FAKE_JWT not in raw
    assert "12345678" not in raw


@pytest.mark.parametrize(
    "stmt",
    [
        "UPDATE audit_events SET actor_id = 'x'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_database_blocks_mutation(session_factory: sessionmaker[Session], stmt: str) -> None:
    _append(session_factory, AuditLog(environment="test"))
    with pytest.raises(DBAPIError, match="append-only"), session_scope(session_factory) as s:
        s.execute(text(stmt))
    with session_scope(session_factory) as s:
        assert s.execute(select(func.count()).select_from(AuditEvent)).scalar_one() == 1


def test_tampering_is_detected(session_factory: sessionmaker[Session]) -> None:
    log = AuditLog(environment="test")
    _append(session_factory, log, n=3)
    # Simulate an attacker with table-owner rights who disables the trigger and edits a row.
    with session_scope(session_factory) as s:
        s.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        s.execute(text("UPDATE audit_events SET payload = '{\"i\": 99}' WHERE id = 2"))
        s.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))
    with session_scope(session_factory) as s:
        res = AuditLog.verify(s)
        assert not res.ok
        assert res.first_bad_id == 2
        assert res.reason is not None and "altered" in res.reason
        with pytest.raises(AuditIntegrityError):
            AuditLog.verify(s, raise_on_failure=True)


def test_deleted_row_is_detected(session_factory: sessionmaker[Session]) -> None:
    _append(session_factory, AuditLog(environment="test"), n=3)
    with session_scope(session_factory) as s:
        s.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete"))
        s.execute(text("DELETE FROM audit_events WHERE id = 2"))
        s.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete"))
    with session_scope(session_factory) as s:
        res = AuditLog.verify(s)
    assert not res.ok and res.first_bad_id == 3


def test_concurrent_appends_keep_a_single_chain(session_factory: sessionmaker[Session]) -> None:
    log = AuditLog(environment="test")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: _append(session_factory, log, n=10), range(8)))
    with session_scope(session_factory) as s:
        res = AuditLog.verify(s)
    assert res.ok and res.rows_checked == 80


@pytest.mark.parametrize("bad", ["NoDots", "system", "System.Started", "a..b", "a.b-c"])
def test_invalid_event_types_rejected(session_factory: sessionmaker[Session], bad: str) -> None:
    with pytest.raises(ValueError, match="event_type"), session_scope(session_factory) as s:
        AuditLog(environment="test").append(
            s, event_type=bad, actor_type=ActorType.SYSTEM, actor_id="x"
        )


def test_failed_transaction_leaves_no_audit_row(session_factory: sessionmaker[Session]) -> None:
    log = AuditLog(environment="test")
    with pytest.raises(RuntimeError), session_scope(session_factory) as s:
        log.append(s, event_type="test.event", actor_type=ActorType.SYSTEM, actor_id="x")
        raise RuntimeError("business operation failed")
    with session_scope(session_factory) as s:
        assert s.execute(select(func.count()).select_from(AuditEvent)).scalar_one() == 0
