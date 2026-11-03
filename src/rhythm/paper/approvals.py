"""Approval queue for REQUIRES_REVIEW candidates (PAPER, decision D-06).

Candidates the rules produced and the risk engine accepted go through automatically. Only
candidates marked REQUIRES_REVIEW — by the risk engine (order value above the review
threshold) or by the research gate — wait here. Approving one re-runs every risk check
against current data before any PAPER order exists (see ``service.decide_approval``).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.errors import RhythmError
from rhythm.core.market import Horizon
from rhythm.db.models import ApprovalRow, StrategySignalRow
from rhythm.execution.runner import SignalRecord
from rhythm.notifications.service import notify
from rhythm.strategies.base import DecisionState, SignalType

APPROVABLE = frozenset({SignalType.ENTER_LONG, SignalType.ADD_LONG})


class ApprovalNotFoundError(RhythmError):
    pass


class ApprovalStateError(RhythmError):
    pass


def expiry(calendar: SessionCalendar, horizon: Horizon, at: datetime) -> datetime:
    """Intraday: the same session's close. Daily horizons: the next session's close."""
    d = at.astimezone(EXCHANGE_TZ).date()
    if horizon is Horizon.INTRADAY:
        close = calendar.session_close(d)
        return close if close > at else at + timedelta(minutes=5)
    n = d + timedelta(days=1)
    while not calendar.is_trading_day(n):
        n += timedelta(days=1)
    return calendar.session_close(n)


def _jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str)) if v is not None else None


def queue(
    session: Session,
    rec: SignalRecord,
    row: StrategySignalRow,
    *,
    account_id: int,
    calendar: SessionCalendar,
    at: datetime,
    audit: AuditLog,
    actor_id: str,
) -> ApprovalRow | None:
    sig = rec.signal
    if rec.decision is not DecisionState.REQUIRES_REVIEW or sig.type not in APPROVABLE:
        return None
    pending = session.execute(
        select(ApprovalRow.id).where(
            ApprovalRow.account_id == account_id,
            ApprovalRow.strategy == sig.strategy,
            ApprovalRow.exchange == sig.key.exchange.value,
            ApprovalRow.symbol == sig.key.symbol,
            ApprovalRow.signal_type == sig.type.value,
            ApprovalRow.status == "PENDING",
            ApprovalRow.expires_at > at,
        )
    ).first()
    if pending is not None:
        return None  # one open request per candidate; repeats of the same idea are not queued
    if rec.advice is not None and not rec.advice.allow:
        source, reason = "RESEARCH", rec.advice.reason
    else:
        source = "RISK"
        reason = "risk policy requires a human decision (order value above the review threshold)"
    a = ApprovalRow(
        created_at=at,
        account_id=account_id,
        signal_id=row.id,
        horizon=sig.horizon.value,
        exchange=sig.key.exchange.value,
        symbol=sig.key.symbol,
        signal_type=sig.type.value,
        strategy=sig.strategy,
        version=sig.version,
        source=source,
        reason=reason[:500],
        detail={
            "decided_at": sig.at.isoformat(),
            "ref_price": str(sig.ref_price),
            "stop": None if sig.stop is None else str(sig.stop),
            "target": None if sig.target is None else str(sig.target),
            "target_weight": None if sig.target_weight is None else str(sig.target_weight),
            "reasons": list(sig.reasons),
            "thesis": _jsonable(sig.thesis),
            "risk": {
                "outcome": rec.risk.outcome.value,
                "quantity": rec.risk.quantity,
                "policy_version": rec.risk.policy_version,
            },
            "assessment_id": rec.advice.ref if rec.advice else None,
        },
        data_is_mock=row.data_is_mock,
        expires_at=expiry(calendar, sig.horizon, at),
    )
    session.add(a)
    session.flush()
    audit.append(
        session,
        event_type="approval.requested",
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        subject_type="approval",
        subject_id=str(a.id),
        payload={"instrument": f"{a.exchange}:{a.symbol}", "type": a.signal_type, "source": source},
    )
    exp = a.expires_at.astimezone(EXCHANGE_TZ)
    notify(
        session,
        kind="approval_pending",
        severity="warning",
        title=f"Approval needed: {a.signal_type} {a.exchange}:{a.symbol} (PAPER)",
        body=f"{a.strategy}: {reason[:300]} Expires {exp:%d %b %H:%M} IST.",
        at=at,
        link="/approvals",
        subject_type="approval",
        subject_id=str(a.id),
    )
    return a


def expire_due(session: Session, at: datetime, audit: AuditLog) -> int:
    n = 0
    for a in session.execute(
        select(ApprovalRow).where(ApprovalRow.status == "PENDING", ApprovalRow.expires_at <= at)
    ).scalars():
        a.status, a.decided_at, a.decided_by = "EXPIRED", at, "system"
        a.decision_reason = "not decided before expiry"
        audit.append(
            session,
            event_type="approval.expired",
            actor_type=ActorType.SYSTEM,
            actor_id="approvals",
            subject_type="approval",
            subject_id=str(a.id),
        )
        n += 1
    return n


def as_dict(a: ApprovalRow) -> dict[str, Any]:
    return {
        "id": a.id,
        "created_at": a.created_at,
        "account_id": a.account_id,
        "signal_id": a.signal_id,
        "instrument": f"{a.exchange}:{a.symbol}",
        "horizon": a.horizon,
        "signal_type": a.signal_type,
        "strategy": f"{a.strategy}@{a.version}",
        "source": a.source,
        "reason": a.reason,
        "detail": a.detail,
        "data_is_mock": a.data_is_mock,
        "expires_at": a.expires_at,
        "status": a.status,
        "decided_at": a.decided_at,
        "decided_by": a.decided_by,
        "decision_reason": a.decision_reason,
        "decision_risk": a.decision_risk,
        "order_id": a.order_id,
    }
