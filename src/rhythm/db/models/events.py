"""Corporate events and event-calendar syncs (coverage proof for the R18 gate)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base


class CorporateEventRow(Base):
    __tablename__ = "corporate_events"
    __table_args__ = (
        UniqueConstraint(
            "exchange", "symbol", "kind", "event_date", "source", name="uq_corporate_events"
        ),
        Index("ix_corporate_events_lookup", "exchange", "symbol", "event_date"),
        CheckConstraint(
            "kind IN ('RESULTS','DIVIDEND','SPLIT','BONUS','AGM','BOARD_MEETING','OTHER')",
            name="kind",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    note: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EventSyncRow(Base):
    """A completed calendar sync for one instrument: proof the calendar was checked."""

    __tablename__ = "event_syncs"
    __table_args__ = (Index("ix_event_syncs_lookup", "exchange", "symbol", "synced_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    events_found: Mapped[int] = mapped_column(Integer, nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
