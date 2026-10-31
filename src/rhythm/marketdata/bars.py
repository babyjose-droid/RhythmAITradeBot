"""Bar storage: validated, provenance-tagged, idempotent upserts; MOCK refused in real envs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from rhythm.config import Environment
from rhythm.core.clock import Clock
from rhythm.core.market import InstrumentKey
from rhythm.db.models import MarketBar
from rhythm.instruments.registry import ensure_source_allowed
from rhythm.marketdata.quality import QualityKind, Severity, record_quality_event
from rhythm.marketdata.types import Bar, Interval
from rhythm.marketdata.validation import bar_issues


@dataclass(frozen=True)
class UpsertResult:
    written: int
    rejected: int


def upsert_bars(
    session: Session, bars: Sequence[Bar], *, clock: Clock, environment: Environment
) -> UpsertResult:
    now = clock.now()
    rows: list[dict[str, object]] = []
    rejected = 0
    for b in bars:
        ensure_source_allowed(b.source, environment)
        issues = bar_issues(b, now)
        if issues:
            rejected += 1
            record_quality_event(
                session,
                clock,
                source=b.source.value,
                kind=QualityKind.INVALID_BAR,
                severity=Severity.WARNING,
                key=b.key,
                detail={
                    "interval": b.interval.value,
                    "start": b.start.isoformat(),
                    "issues": issues,
                },
            )
            continue
        rows.append(
            {
                "exchange": b.key.exchange.value,
                "symbol": b.key.symbol,
                "interval": b.interval.value,
                "bar_start": b.start,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "source": b.source.value,
                "is_mock": b.is_mock,
                "received_at": b.received_ts,
            }
        )
    if rows:
        stmt = insert(MarketBar).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_bar",
            set_={
                c: stmt.excluded[c]
                for c in ("open", "high", "low", "close", "volume", "received_at")
            },
        )
        session.execute(stmt)
    return UpsertResult(written=len(rows), rejected=rejected)


def get_bars(
    session: Session,
    key: InstrumentKey,
    interval: Interval,
    start: datetime,
    end: datetime,
    *,
    limit: int = 5000,
) -> list[MarketBar]:
    stmt = (
        select(MarketBar)
        .where(
            MarketBar.exchange == key.exchange.value,
            MarketBar.symbol == key.symbol,
            MarketBar.interval == interval.value,
            MarketBar.bar_start >= start,
            MarketBar.bar_start < end,
        )
        .order_by(MarketBar.bar_start)
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def latest_bar(session: Session, key: InstrumentKey, interval: Interval) -> MarketBar | None:
    """Most recent stored bar (any source), so callers can report its provenance."""
    return session.execute(
        select(MarketBar)
        .where(
            MarketBar.exchange == key.exchange.value,
            MarketBar.symbol == key.symbol,
            MarketBar.interval == interval.value,
        )
        .order_by(MarketBar.bar_start.desc(), MarketBar.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_bar_start(session: Session, key: InstrumentKey, interval: Interval) -> datetime | None:
    bar = latest_bar(session, key, interval)
    return None if bar is None else bar.bar_start
