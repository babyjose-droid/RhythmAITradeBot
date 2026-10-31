"""Instrument registry persistence: snapshot application and point-in-time queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.config import Environment
from rhythm.core.clock import Clock
from rhythm.core.errors import RhythmError
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.db.models import Instrument, InstrumentSnapshot
from rhythm.instruments.snapshot import (
    DEFAULT_MAX_REMOVAL_RATIO,
    SnapshotRejectedError,
    content_hash,
    diff_snapshot,
)
from rhythm.instruments.types import ScripRecord
from rhythm.marketdata.quality import QualityKind, Severity, record_quality_event

REAL_DATA_ENVIRONMENTS = (Environment.STAGING, Environment.PRODUCTION)


class MockDataNotAllowedError(RhythmError):
    """Mock data may not be written in staging/production."""


def ensure_source_allowed(source: DataOrigin, environment: Environment) -> None:
    if source is DataOrigin.MOCK and environment in REAL_DATA_ENVIRONMENTS:
        raise MockDataNotAllowedError(f"MOCK data is not allowed in {environment.value}")


@dataclass(frozen=True)
class SnapshotResult:
    snapshot_id: int
    status: str
    summary: dict[str, int]
    already_applied: bool = False


def _attrs(row: Instrument) -> dict[str, str | int | None]:
    return {
        "source_code": row.source_code,
        "instrument_type": row.instrument_type,
        "tick_size": str(row.tick_size.normalize()),
        "lot_size": row.lot_size,
        "isin": row.isin,
        "name": row.name,
    }


def active_instruments(session: Session, exchange: Exchange) -> dict[str, Instrument]:
    rows = session.execute(
        select(Instrument).where(
            Instrument.exchange == exchange.value, Instrument.valid_to.is_(None)
        )
    ).scalars()
    return {r.symbol: r for r in rows}


def _new_row(r: ScripRecord, as_of: date, snapshot_id: int) -> Instrument:
    return Instrument(
        exchange=r.exchange.value,
        symbol=r.symbol,
        source=r.source.value,
        source_code=r.source_code,
        is_mock=r.source is DataOrigin.MOCK,
        instrument_type=r.instrument_type,
        tick_size=r.tick_size,
        lot_size=r.lot_size,
        isin=r.isin,
        name=r.name,
        valid_from=as_of,
        snapshot_id=snapshot_id,
    )


def apply_snapshot(
    session: Session,
    *,
    records: Sequence[ScripRecord],
    exchange: Exchange,
    source: DataOrigin,
    as_of: date,
    clock: Clock,
    audit: AuditLog,
    environment: Environment,
    actor_id: str = "system",
    max_removal_ratio: float = DEFAULT_MAX_REMOVAL_RATIO,
) -> SnapshotResult:
    """Apply a scrip-master snapshot inside the caller's transaction.

    Raises SnapshotRejectedError (after recording the rejection) when the snapshot is unsafe.
    The caller must commit even on rejection so the REJECTED record and quality event persist.
    """
    ensure_source_allowed(source, environment)
    if any(r.source is not source for r in records):
        raise SnapshotRejectedError("records from mixed sources")
    digest = content_hash(records) if records else "0" * 64
    rows_json = [{"symbol": r.symbol, **r.attributes()} for r in records]

    existing = session.execute(
        select(InstrumentSnapshot).where(
            InstrumentSnapshot.source == source.value,
            InstrumentSnapshot.exchange == exchange.value,
            InstrumentSnapshot.as_of_date == as_of,
            InstrumentSnapshot.content_hash == digest,
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status != "REJECTED":
        return SnapshotResult(existing.id, existing.status, existing.summary, already_applied=True)

    def reject(reason: str, detail: dict[str, object]) -> SnapshotRejectedError:
        snap = InstrumentSnapshot(
            source=source.value,
            exchange=exchange.value,
            as_of_date=as_of,
            received_at=clock.now(),
            row_count=len(records),
            content_hash=digest,
            status="REJECTED",
            summary={"reason": reason},
            rows=rows_json,
        )
        if existing is None:
            session.add(snap)
        record_quality_event(
            session,
            clock,
            source=source.value,
            kind=QualityKind.SNAPSHOT_REJECTED,
            severity=Severity.CRITICAL,
            exchange=exchange.value,
            detail={"reason": reason, "as_of": as_of.isoformat(), **detail},
        )
        session.flush()
        return SnapshotRejectedError(reason, detail)

    latest_applied = session.execute(
        select(func.max(InstrumentSnapshot.as_of_date)).where(
            InstrumentSnapshot.source == source.value,
            InstrumentSnapshot.exchange == exchange.value,
            InstrumentSnapshot.status.in_(("APPLIED", "NO_CHANGE")),
        )
    ).scalar_one_or_none()
    if latest_applied is not None and as_of < latest_applied:
        raise reject("out-of-order snapshot", {"latest_applied": latest_applied.isoformat()})
    if latest_applied is not None and as_of == latest_applied:
        raise reject("a different snapshot was already applied for this date", {})

    active = active_instruments(session, exchange)
    try:
        diff = diff_snapshot(
            {s: _attrs(r) for s, r in active.items()},
            records,
            exchange,
            max_removal_ratio=max_removal_ratio,
        )
    except SnapshotRejectedError as exc:
        raise reject(exc.reason, exc.detail) from exc

    summary = diff.summary()
    status = "APPLIED" if (diff.added or diff.changed or diff.removed) else "NO_CHANGE"
    snap = InstrumentSnapshot(
        source=source.value,
        exchange=exchange.value,
        as_of_date=as_of,
        received_at=clock.now(),
        row_count=len(records),
        content_hash=digest,
        status=status,
        summary=summary,
        rows=rows_json,
    )
    session.add(snap)
    session.flush()

    for symbol in diff.removed:
        active[symbol].valid_to = as_of
    for r in diff.changed:
        active[r.symbol].valid_to = as_of
    session.flush()  # close versions before opening new ones (partial unique index)
    for r in [*diff.added, *diff.changed]:
        session.add(_new_row(r, as_of, snap.id))
    session.flush()

    audit.append(
        session,
        event_type="instruments.snapshot_applied",
        actor_type=ActorType.SYSTEM if actor_id == "system" else ActorType.USER,
        actor_id=actor_id,
        subject_type="instrument_snapshot",
        subject_id=str(snap.id),
        payload={
            "source": source.value,
            "exchange": exchange.value,
            "as_of": as_of.isoformat(),
            "status": status,
            **summary,
        },
    )
    return SnapshotResult(snap.id, status, summary)


def instruments_as_of(
    session: Session,
    exchange: Exchange,
    on: date,
    *,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Instrument]:
    """Instruments that were active on ``on`` (point-in-time; avoids survivorship bias)."""
    stmt = select(Instrument).where(
        Instrument.exchange == exchange.value,
        Instrument.valid_from <= on,
        or_(Instrument.valid_to.is_(None), Instrument.valid_to > on),
    )
    if search:
        pattern = f"%{search.upper()}%"
        stmt = stmt.where(
            or_(Instrument.symbol.like(pattern), func.upper(Instrument.name).like(pattern))
        )
    stmt = stmt.order_by(Instrument.symbol).limit(limit).offset(offset)
    return list(session.execute(stmt).scalars())


def get_active(session: Session, key: InstrumentKey) -> Instrument | None:
    return session.execute(
        select(Instrument).where(
            and_(
                Instrument.exchange == key.exchange.value,
                Instrument.symbol == key.symbol,
                Instrument.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
