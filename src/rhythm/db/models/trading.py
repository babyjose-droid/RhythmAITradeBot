"""PAPER accounts, orders, order events, fills, positions, strategy signals and backtests.

``account_kind`` is constrained to 'PAPER' at the database level. Live orders cannot be
stored until a Phase 10 migration deliberately changes that constraint.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from rhythm.db.base import Base, TimestampMixin

MONEY = Numeric(20, 4)
HORIZON_CHECK = "horizon IN ('INTRADAY', 'SWING', 'LONG_TERM')"


class PaperAccount(TimestampMixin, Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (CheckConstraint("starting_cash > 0", name="starting_cash"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    starting_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class Ledger(TimestampMixin, Base):
    """A virtual per-horizon ledger inside a PAPER account (MULTI_HORIZON_PORTFOLIO.md §1)."""

    __tablename__ = "ledgers"
    __table_args__ = (
        CheckConstraint("account_kind = 'PAPER'", name="paper_only"),
        CheckConstraint(HORIZON_CHECK, name="horizon"),
        CheckConstraint("allocated_capital >= 0", name="allocation"),
        UniqueConstraint("account_id", "horizon", name="uq_ledgers_account_horizon"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    allocated_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("account_kind = 'PAPER'", name="paper_only"),
        CheckConstraint("quantity > 0 AND filled_quantity >= 0", name="qty"),
        CheckConstraint("filled_quantity <= quantity", name="fill_le_qty"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="side"),
        CheckConstraint(HORIZON_CHECK, name="horizon"),
        Index("ix_orders_account_state", "account_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False, server_default="INTRADAY")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column(String(8), nullable=False)
    product: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    average_fill_price: Mapped[Decimal | None] = mapped_column(MONEY)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    oco_group: Mapped[str | None] = mapped_column(String(120))
    strategy: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(200))
    venue_order_id: Mapped[str | None] = mapped_column(String(40))
    data_is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderEventRow(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(24))
    to_state: Mapped[str] = mapped_column(String(24), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (
        CheckConstraint("account_kind = 'PAPER'", name="paper_only"),
        CheckConstraint("quantity > 0 AND price > 0", name="positive"),
        CheckConstraint(HORIZON_CHECK, name="horizon"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    account_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False, server_default="INTRADAY")
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    charges_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    charges: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperPosition(Base):
    __tablename__ = "paper_positions"
    __table_args__ = (
        CheckConstraint(HORIZON_CHECK, name="horizon"),
        CheckConstraint("quantity >= 0", name="long_only"),
    )

    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), primary_key=True)
    horizon: Mapped[str] = mapped_column(String(16), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(8), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    average_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    charges_paid: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop: Mapped[Decimal | None] = mapped_column(MONEY)
    target: Mapped[Decimal | None] = mapped_column(MONEY)


class StrategySignalRow(Base):
    """A candidate decision. Never an order; ``order_id`` links one if it was created."""

    __tablename__ = "strategy_signals"
    __table_args__ = (
        CheckConstraint("context IN ('PAPER', 'RESEARCH')", name="context"),
        Index("ix_strategy_signals_account", "account_id", "decided_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    context: Mapped[str] = mapped_column(String(10), nullable=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("paper_accounts.id"))
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    decision_state: Mapped[str] = mapped_column(String(24), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ref_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stop: Mapped[Decimal | None] = mapped_column(MONEY)
    target: Mapped[Decimal | None] = mapped_column(MONEY)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    risk: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    data_is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    params_hash: Mapped[str] = mapped_column(String(16), nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cost_model: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fill_model: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    run_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    data_is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False)
    bars_processed: Mapped[int] = mapped_column(Integer, nullable=False)
    signals_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    splits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (UniqueConstraint("run_id", "split", "seq", name="uq_backtest_trade"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    split: Mapped[str] = mapped_column(String(32), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    charges: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    exit_reason: Mapped[str] = mapped_column(String(16), nullable=False)
