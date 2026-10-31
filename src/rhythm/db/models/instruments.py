"""Point-in-time instrument registry, scrip-master snapshots and watchlists."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from rhythm.db.base import Base, TimestampMixin


class InstrumentSnapshot(Base):
    __tablename__ = "instrument_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "exchange", "as_of_date", "content_hash", name="uq_snapshot"),
        CheckConstraint("status IN ('APPLIED', 'REJECTED', 'NO_CHANGE')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


class Instrument(TimestampMixin, Base):
    """One row per (exchange, symbol) *version*. valid_to IS NULL means currently active."""

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint("tick_size > 0", name="tick_positive"),
        CheckConstraint("lot_size >= 1", name="lot_positive"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="validity"),
        Index(
            "uq_instruments_active",
            "exchange",
            "symbol",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        Index("ix_instruments_exchange_symbol", "exchange", "symbol"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_code: Mapped[str] = mapped_column(String(32), nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12))
    name: Mapped[str | None] = mapped_column(String(200))
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("instrument_snapshots.id"), nullable=False)


class Watchlist(TimestampMixin, Base):
    __tablename__ = "watchlists"
    __table_args__ = (
        CheckConstraint("horizon IN ('INTRADAY', 'SWING', 'LONG_TERM', 'ANY')", name="horizon"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    items: Mapped[list[WatchlistItem]] = relationship(
        back_populates="watchlist",
        cascade="all, delete-orphan",
        order_by="WatchlistItem.id",
    )


class WatchlistItem(Base):
    """Items reference (exchange, symbol), not an instrument row: versions change over time."""

    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("watchlist_id", "exchange", "symbol", name="uq_item"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(
        ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500))
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    watchlist: Mapped[Watchlist] = relationship(back_populates="items")
