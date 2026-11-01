"""Persisted event calendar: sync from a provider, manual entries, point-in-time queries."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.config import Environment
from rhythm.core.clock import EXCHANGE_TZ, ensure_utc
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.db.models.events import CorporateEventRow, EventSyncRow
from rhythm.events.types import CorporateEvent, EventKind, EventProvider
from rhythm.instruments.registry import ensure_source_allowed

SYNC_HORIZON_DAYS = 120


def _to_event(r: CorporateEventRow) -> CorporateEvent:
    return CorporateEvent(
        key=InstrumentKey(Exchange(r.exchange), r.symbol),
        kind=EventKind(r.kind),
        event_date=r.event_date,
        known_at=r.known_at,
        source=r.source,
        source_ref=r.source_ref,
        note=r.note,
    )


def _upsert(session: Session, e: CorporateEvent) -> bool:
    src = e.source.value if isinstance(e.source, DataOrigin) else str(e.source)
    existing = session.execute(
        select(CorporateEventRow).where(
            CorporateEventRow.exchange == e.key.exchange.value,
            CorporateEventRow.symbol == e.key.symbol,
            CorporateEventRow.kind == e.kind.value,
            CorporateEventRow.event_date == e.event_date,
            CorporateEventRow.source == src,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False
    session.add(
        CorporateEventRow(
            exchange=e.key.exchange.value,
            symbol=e.key.symbol,
            kind=e.kind.value,
            event_date=e.event_date,
            known_at=e.known_at,
            source=src,
            source_ref=e.source_ref[:120],
            note=e.note[:300],
            is_mock=e.is_mock,
        )
    )
    return True


def sync(
    session: Session,
    provider: EventProvider,
    keys: list[InstrumentKey],
    *,
    now: datetime,
    environment: Environment,
    audit: AuditLog,
    actor_id: str,
) -> dict[str, int]:
    """Fetch events for the next SYNC_HORIZON_DAYS and record coverage per instrument."""
    ensure_source_allowed(provider.origin, environment)
    today = ensure_utc(now).astimezone(EXCHANGE_TZ).date()
    end = today + timedelta(days=SYNC_HORIZON_DAYS)
    out: dict[str, int] = {}
    for k in keys:
        found = [e for e in provider.events(k, today, end) if e.known_at <= ensure_utc(now)]
        added = sum(_upsert(session, e) for e in found)
        session.add(
            EventSyncRow(
                exchange=k.exchange.value,
                symbol=k.symbol,
                source=provider.origin.value,
                synced_at=now,
                window_start=today,
                window_end=end,
                events_found=len(found),
                is_mock=provider.origin is DataOrigin.MOCK,
            )
        )
        out[str(k)] = added
    session.flush()
    audit.append(
        session,
        event_type="events.synced",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        payload={"source": provider.origin.value, "instruments": len(keys), "added": out},
    )
    return out


def add_manual(session: Session, event: CorporateEvent, *, audit: AuditLog, actor_id: str) -> bool:
    """A user-entered event. It adds information but never counts as calendar coverage."""
    added = _upsert(session, event)
    session.flush()
    audit.append(
        session,
        event_type="events.manual_added",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="instrument",
        subject_id=str(event.key),
        payload={"kind": event.kind.value, "date": event.event_date.isoformat(), "added": added},
    )
    return added


def last_sync(session: Session, key: InstrumentKey, as_of: datetime) -> datetime | None:
    return session.execute(
        select(EventSyncRow.synced_at)
        .where(
            EventSyncRow.exchange == key.exchange.value,
            EventSyncRow.symbol == key.symbol,
            EventSyncRow.synced_at <= as_of,
        )
        .order_by(EventSyncRow.synced_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def known_events(
    session: Session, key: InstrumentKey, as_of: datetime, start: date, end: date
) -> list[CorporateEvent]:
    rows = session.execute(
        select(CorporateEventRow)
        .where(
            CorporateEventRow.exchange == key.exchange.value,
            CorporateEventRow.symbol == key.symbol,
            CorporateEventRow.event_date >= start,
            CorporateEventRow.event_date <= end,
            CorporateEventRow.known_at <= as_of,
        )
        .order_by(CorporateEventRow.event_date)
    ).scalars()
    return [_to_event(r) for r in rows]


def list_events(
    session: Session, keys: list[InstrumentKey], start: date, end: date
) -> list[CorporateEventRow]:
    if not keys:
        return []
    out: list[CorporateEventRow] = []
    for k in keys:
        out += session.execute(
            select(CorporateEventRow)
            .where(
                CorporateEventRow.exchange == k.exchange.value,
                CorporateEventRow.symbol == k.symbol,
                CorporateEventRow.event_date >= start,
                CorporateEventRow.event_date <= end,
            )
            .order_by(CorporateEventRow.event_date)
        ).scalars()
    return sorted(out, key=lambda r: (r.event_date, r.symbol))
