"""Quarterly fundamentals as published (point-in-time; restatements are new rows)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Identity,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base


class FundamentalSnapshotRow(Base):
    __tablename__ = "fundamental_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "exchange",
            "symbol",
            "period_end",
            "source",
            "known_at",
            name="uq_fundamental_snapshots",
        ),
        Index("ix_fundamental_snapshots_lookup", "exchange", "symbol", "known_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    known_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
