"""Persisted fundamentals: sync from a provider and point-in-time reads."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.config import Environment
from rhythm.core.clock import EXCHANGE_TZ, ensure_utc
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.db.models.fundamentals import FundamentalSnapshotRow
from rhythm.fundamentals.types import FundamentalSnapshot, FundamentalsProvider
from rhythm.instruments.registry import ensure_source_allowed

HISTORY_START = date(2018, 1, 1)


def _to_snapshot(r: FundamentalSnapshotRow) -> FundamentalSnapshot:
    return FundamentalSnapshot(
        key=InstrumentKey(Exchange(r.exchange), r.symbol),
        period_end=r.period_end,
        known_at=r.known_at,
        source=r.source,
        source_ref=r.source_ref,
        metrics={k: Decimal(v) for k, v in r.metrics.items()},
    )


def sync(
    session: Session,
    provider: FundamentalsProvider,
    keys: list[InstrumentKey],
    *,
    now: datetime,
    environment: Environment,
    audit: AuditLog,
    actor_id: str,
) -> dict[str, int]:
    """Store every snapshot already published at ``now``. Existing rows are never changed."""
    ensure_source_allowed(provider.origin, environment)
    now = ensure_utc(now)
    today = now.astimezone(EXCHANGE_TZ).date()
    out: dict[str, int] = {}
    for k in keys:
        have = {
            (r.period_end, r.source, r.known_at)
            for r in session.execute(
                select(FundamentalSnapshotRow).where(
                    FundamentalSnapshotRow.exchange == k.exchange.value,
                    FundamentalSnapshotRow.symbol == k.symbol,
                )
            ).scalars()
        }
        added = 0
        for s in provider.snapshots(k, HISTORY_START, today):
            if s.known_at > now or (s.period_end, s.source, s.known_at) in have:
                continue
            session.add(
                FundamentalSnapshotRow(
                    exchange=k.exchange.value,
                    symbol=k.symbol,
                    period_end=s.period_end,
                    known_at=s.known_at,
                    source=s.source,
                    source_ref=s.source_ref[:120],
                    metrics={m: str(v) for m, v in s.metrics.items()},
                    is_mock=s.is_mock,
                )
            )
            added += 1
        out[str(k)] = added
    session.flush()
    audit.append(
        session,
        event_type="fundamentals.synced",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        payload={"source": provider.origin.value, "added": out},
    )
    return out


def known(session: Session, key: InstrumentKey, as_of: datetime) -> list[FundamentalSnapshot]:
    rows = session.execute(
        select(FundamentalSnapshotRow)
        .where(
            FundamentalSnapshotRow.exchange == key.exchange.value,
            FundamentalSnapshotRow.symbol == key.symbol,
            FundamentalSnapshotRow.known_at <= as_of,
        )
        .order_by(FundamentalSnapshotRow.period_end, FundamentalSnapshotRow.known_at)
    ).scalars()
    return [_to_snapshot(r) for r in rows]
