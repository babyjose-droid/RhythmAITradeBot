from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from rhythm.audit import AuditLog
from rhythm.broker.mock import mock_scrip_master
from rhythm.config import Environment
from rhythm.core.clock import FixedClock
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.db.models import AuditEvent, DataQualityEvent, Instrument, InstrumentSnapshot
from rhythm.db.session import session_scope
from rhythm.instruments.registry import (
    MockDataNotAllowedError,
    apply_snapshot,
    get_active,
    instruments_as_of,
)
from rhythm.instruments.snapshot import SnapshotRejectedError
from rhythm.instruments.types import ScripRecord

pytestmark = pytest.mark.integration

CLOCK = FixedClock(datetime(2026, 10, 30, 12, 0, tzinfo=UTC))
AUDIT = AuditLog(environment="test", clock=CLOCK)
BASE = mock_scrip_master(Exchange.NSE)
D1, D2, D3 = date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)


def apply(
    sf: sessionmaker[Session],
    records: list[ScripRecord],
    as_of: date,
    *,
    env: Environment = Environment.TEST,
) -> object:
    with session_scope(sf) as s:
        try:
            return apply_snapshot(
                s,
                records=records,
                exchange=Exchange.NSE,
                source=DataOrigin.MOCK,
                as_of=as_of,
                clock=CLOCK,
                audit=AUDIT,
                environment=env,
            )
        except SnapshotRejectedError as exc:
            return exc  # commit the rejection record


def test_first_snapshot_and_idempotent_reapply(session_factory: sessionmaker[Session]) -> None:
    r1 = apply(session_factory, BASE, D1)
    r2 = apply(session_factory, BASE, D1)
    assert r1.status == "APPLIED" and r1.summary["added"] == 20  # type: ignore[attr-defined]
    assert r2.already_applied and r2.snapshot_id == r1.snapshot_id  # type: ignore[attr-defined]
    with session_scope(session_factory) as s:
        assert len(instruments_as_of(s, Exchange.NSE, D1, limit=1000)) == 20
        types = s.execute(select(AuditEvent.event_type)).scalars().all()
        assert types == ["instruments.snapshot_applied"]


def test_point_in_time_versions(session_factory: sessionmaker[Session]) -> None:
    apply(session_factory, BASE, D1)
    day2 = [replace(BASE[0], tick_size=Decimal("0.10")), *BASE[2:]]
    day2.append(replace(BASE[2], symbol="MOCKNEW", source_code="990001"))
    r = apply(session_factory, day2, D2)
    assert r.summary == {"added": 1, "changed": 1, "removed": 1, "unchanged": 18}  # type: ignore[attr-defined]
    with session_scope(session_factory) as s:
        on_d1 = {i.symbol: i for i in instruments_as_of(s, Exchange.NSE, D1, limit=1000)}
        on_d2 = {i.symbol: i for i in instruments_as_of(s, Exchange.NSE, D2, limit=1000)}
        assert "MOCKB" in on_d1 and "MOCKB" not in on_d2  # delisted on D2
        assert "MOCKNEW" not in on_d1 and "MOCKNEW" in on_d2
        assert on_d1["MOCKA"].tick_size == Decimal("0.05")
        assert on_d2["MOCKA"].tick_size == Decimal("0.10")
        versions = s.execute(select(Instrument).where(Instrument.symbol == "MOCKA")).scalars().all()
        assert len(versions) == 2
        active = get_active(s, InstrumentKey(Exchange.NSE, "MOCKA"))
        assert active is not None and active.valid_from == D2


def test_unchanged_next_day_is_no_change(session_factory: sessionmaker[Session]) -> None:
    apply(session_factory, BASE, D1)
    r = apply(session_factory, BASE, D2)
    assert r.status == "NO_CHANGE"  # type: ignore[attr-defined]


def _rejections(sf: sessionmaker[Session]) -> list[tuple[str, str]]:
    with session_scope(sf) as s:
        snaps = s.execute(
            select(InstrumentSnapshot.status, InstrumentSnapshot.summary).where(
                InstrumentSnapshot.status == "REJECTED"
            )
        ).all()
        dq = s.execute(select(DataQualityEvent.kind)).scalars().all()
    assert dq.count("SNAPSHOT_REJECTED") == len(snaps)
    return [(st, sm["reason"]) for st, sm in snaps]


def test_mass_removal_rejected_and_recorded(session_factory: sessionmaker[Session]) -> None:
    apply(session_factory, BASE, D1)
    r = apply(session_factory, BASE[:5], D2)
    assert isinstance(r, SnapshotRejectedError)
    assert _rejections(session_factory) == [("REJECTED", "too many removals; review required")]
    with session_scope(session_factory) as s:  # registry untouched
        assert len(instruments_as_of(s, Exchange.NSE, D2, limit=1000)) == 20


def test_out_of_order_and_same_day_conflict_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    apply(session_factory, BASE, D2)
    assert isinstance(apply(session_factory, BASE[:19], D1), SnapshotRejectedError)
    changed = [replace(BASE[0], name="Renamed"), *BASE[1:]]
    assert isinstance(apply(session_factory, changed, D2), SnapshotRejectedError)
    reasons = {r for _, r in _rejections(session_factory)}
    assert reasons == {
        "out-of-order snapshot",
        "a different snapshot was already applied for this date",
    }
    assert apply(session_factory, changed, D3).status == "APPLIED"  # type: ignore[attr-defined]


def test_mock_snapshot_refused_in_production(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(MockDataNotAllowedError):
        apply(session_factory, BASE, D1, env=Environment.PRODUCTION)


def test_search(session_factory: sessionmaker[Session]) -> None:
    apply(session_factory, BASE, D1)
    with session_scope(session_factory) as s:
        hits = instruments_as_of(s, Exchange.NSE, D1, search="mockc")
        assert [h.symbol for h in hits] == ["MOCKC"]
