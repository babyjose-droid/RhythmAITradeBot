"""Thesis tracking for multi-day PAPER positions.

A swing entry is created together with its thesis: why it was taken and which conditions
invalidate it. Status flow::

    PENDING_ENTRY --(entry fills)--> OPEN --(strategy exit signal)--> INVALIDATED
         |                             |                                   |
         +--(entry never fills)--------+------(position flat)--------------+--> CLOSED

Every change is written to the append-only ``thesis_events`` table with its evidence.
The thesis is evaluated each session (``last_checked_at``/``last_check``), not only when
something changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm.core.market import Exchange, Horizon, InstrumentKey
from rhythm.db.models import OrderRow, ThesisEventRow, ThesisRow
from rhythm.execution.book import ACCOUNT_KIND, PaperBook
from rhythm.execution.runner import SignalRecord
from rhythm.orders.state import OrderState
from rhythm.strategies.base import SignalType

OPEN_STATES = ("PENDING_ENTRY", "OPEN", "INVALIDATED")


def _event(
    session: Session,
    th: ThesisRow,
    to: str,
    reason: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        ThesisEventRow(
            thesis_id=th.id,
            at=at,
            from_status=th.status,
            to_status=to,
            reason=reason[:300],
            detail=detail or {},
        )
    )
    th.status = to


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _jsonable(v)
        elif isinstance(v, list | tuple):
            out[k] = [
                x if isinstance(x, str | int | float | bool) or x is None else str(x) for x in v
            ]
        elif isinstance(v, str | int | float | bool) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


def open_theses(
    session: Session, account_id: int, horizon: Horizon, key: InstrumentKey | None = None
) -> list[ThesisRow]:
    stmt = select(ThesisRow).where(
        ThesisRow.account_id == account_id,
        ThesisRow.horizon == horizon.value,
        ThesisRow.status.in_(OPEN_STATES),
    )
    if key is not None:
        stmt = stmt.where(ThesisRow.exchange == key.exchange.value, ThesisRow.symbol == key.symbol)
    return list(session.execute(stmt.order_by(ThesisRow.id)).scalars())


def record_signals(
    session: Session,
    *,
    account_id: int,
    horizon: Horizon,
    records: Iterable[SignalRecord],
    new_order_ids: set[str],
    data_is_mock: bool,
    at: datetime,
) -> tuple[int, int]:
    """Create theses for new entries; mark open theses invalidated on exit signals."""
    created = invalidated = 0
    for rec in records:
        s = rec.signal
        if s.type is SignalType.ENTER_LONG and rec.order_id in new_order_ids and s.thesis:
            th = ThesisRow(
                account_kind=ACCOUNT_KIND,
                account_id=account_id,
                horizon=horizon.value,
                exchange=s.key.exchange.value,
                symbol=s.key.symbol,
                strategy=s.strategy,
                version=s.version,
                entry_order_id=rec.order_id,
                status="PENDING_ENTRY",
                rationale=[str(x) for x in s.thesis.get("rationale", [])],
                invalidation=[_jsonable(dict(x)) for x in s.thesis.get("invalidation", [])],
                ref_price=s.ref_price,
                stop=s.stop,
                target=s.target,
                opened_at=s.at,
                last_check={},
                data_is_mock=data_is_mock,
            )
            session.add(th)
            session.flush()
            session.add(
                ThesisEventRow(
                    thesis_id=th.id,
                    at=at,
                    from_status=None,
                    to_status="PENDING_ENTRY",
                    reason="entry order created",
                    detail={"order_id": rec.order_id, "risk": rec.risk.outcome.value},
                )
            )
            created += 1
        elif s.type is SignalType.EXIT_LONG and rec.order_id is not None:
            for th in open_theses(session, account_id, horizon, s.key):
                if th.status == "OPEN":
                    _event(
                        session,
                        th,
                        "INVALIDATED",
                        "; ".join(s.reasons) or "strategy exit",
                        at,
                        {"exit_order_id": rec.order_id, "decided_at": s.at.isoformat()},
                    )
                    invalidated += 1
    session.flush()
    return created, invalidated


def sync(
    session: Session,
    *,
    account_id: int,
    horizon: Horizon,
    book: PaperBook,
    diagnostics: dict[InstrumentKey, dict[str, Any]],
    at: datetime,
    next_review_at: datetime | None,
) -> dict[str, int]:
    """Advance thesis status from order/position facts and record this session's check."""
    out = {"opened": 0, "closed": 0, "checked": 0}
    for th in open_theses(session, account_id, horizon):
        key = InstrumentKey(Exchange(th.exchange), th.symbol)
        entry = session.get(OrderRow, th.entry_order_id)
        qty = book.position(key).quantity
        if th.status == "PENDING_ENTRY" and entry is not None:
            if entry.filled_quantity > 0:
                _event(session, th, "OPEN", "entry filled", at, {"filled": entry.filled_quantity})
                out["opened"] += 1
            elif OrderState(entry.state) in (
                OrderState.CANCELLED,
                OrderState.EXPIRED,
                OrderState.BROKER_REJECTED,
            ):
                th.closed_at, th.close_reason = at, f"entry not filled ({entry.state})"
                _event(session, th, "CLOSED", th.close_reason, at)
                out["closed"] += 1
                continue
        if th.status in ("OPEN", "INVALIDATED") and qty == 0:
            last_sell = session.execute(
                select(OrderRow)
                .where(
                    OrderRow.account_id == account_id,
                    OrderRow.horizon == horizon.value,
                    OrderRow.exchange == th.exchange,
                    OrderRow.symbol == th.symbol,
                    OrderRow.side == "SELL",
                    OrderRow.filled_quantity > 0,
                )
                .order_by(OrderRow.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            how = last_sell.purpose if last_sell is not None else "unknown"
            th.closed_at = at
            th.close_reason = f"position closed by {how}"
            _event(
                session,
                th,
                "CLOSED",
                th.close_reason,
                at,
                {"order_id": None if last_sell is None else last_sell.id},
            )
            out["closed"] += 1
            continue
        if key in diagnostics:
            th.last_checked_at = at
            th.last_check = _jsonable(diagnostics[key])
            th.next_review_at = next_review_at
            out["checked"] += 1
    session.flush()
    return out


def thesis_out(th: ThesisRow, events: list[ThesisEventRow] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": th.id,
        "account_kind": th.account_kind,
        "account_id": th.account_id,
        "horizon": th.horizon,
        "instrument": f"{th.exchange}:{th.symbol}",
        "strategy": f"{th.strategy}@{th.version}",
        "status": th.status,
        "rationale": th.rationale,
        "invalidation": th.invalidation,
        "ref_price": str(th.ref_price),
        "stop": None if th.stop is None else str(th.stop),
        "target": None if th.target is None else str(th.target),
        "entry_order_id": th.entry_order_id,
        "opened_at": th.opened_at,
        "last_checked_at": th.last_checked_at,
        "last_check": th.last_check,
        "next_review_at": th.next_review_at,
        "closed_at": th.closed_at,
        "close_reason": th.close_reason,
        "data_is_mock": th.data_is_mock,
    }
    if events is not None:
        out["events"] = [
            {
                "at": e.at,
                "from": e.from_status,
                "to": e.to_status,
                "reason": e.reason,
                "detail": e.detail,
            }
            for e in events
        ]
    return out
