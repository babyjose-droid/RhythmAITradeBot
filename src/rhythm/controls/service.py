"""Persisted risk controls: versioned policies, kill switch, strategy pauses, risk decisions.

Control state is derived from the append-only ``control_events`` table (latest event per
target wins). Every change is also written to the hash-chained audit log. Releasing the kill
switch or resuming a strategy needs a *user* actor and a reason; the system can only
activate/pause (fail-safe direction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.core.errors import RhythmError
from rhythm.db.models import ControlEventRow, RiskCheckRow, RiskPolicyRow
from rhythm.risk.engine import RiskDecision
from rhythm.risk.policy import DEFAULT_POLICY, RiskPolicy

KILL = "KILL_SWITCH"
PAUSE = "STRATEGY_PAUSE"
GLOBAL = "*"


class ControlError(RhythmError):
    pass


@dataclass(frozen=True)
class ControlState:
    kill_switch_active: bool
    kill_switch: dict[str, Any] | None
    paused: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def paused_strategies(self) -> frozenset[str]:
        return frozenset(self.paused)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kill_switch_active": self.kill_switch_active,
            "kill_switch": self.kill_switch,
            "paused_strategies": self.paused,
        }


def _event_dict(e: ControlEventRow) -> dict[str, Any]:
    return {
        "id": e.id,
        "at": e.at.isoformat(),
        "action": e.action,
        "actor_type": e.actor_type,
        "actor_id": e.actor_id,
        "reason": e.reason,
        "detail": e.detail,
    }


# ---------------------------------------------------------------------------- state
def control_state(session: Session) -> ControlState:
    latest = (
        select(func.max(ControlEventRow.id))
        .group_by(ControlEventRow.control, ControlEventRow.target)
        .scalar_subquery()
    )
    rows = session.execute(select(ControlEventRow).where(ControlEventRow.id.in_(latest))).scalars()
    kill: dict[str, Any] | None = None
    active = False
    paused: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.control == KILL:
            kill = _event_dict(r)
            active = r.action == "ACTIVATE"
        elif r.action == "ACTIVATE":
            paused[r.target] = _event_dict(r)
    return ControlState(active, kill, paused)


def control_history(session: Session, limit: int = 100) -> list[dict[str, Any]]:
    rows = session.execute(
        select(ControlEventRow).order_by(ControlEventRow.id.desc()).limit(limit)
    ).scalars()
    return [_event_dict(r) | {"control": r.control, "target": r.target} for r in rows]


def _record(
    session: Session,
    audit: AuditLog,
    *,
    control: str,
    target: str,
    action: str,
    actor_type: ActorType,
    actor_id: str,
    reason: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
) -> ControlEventRow:
    reason = reason.strip()
    if not reason:
        raise ControlError("a reason is required")
    if action == "RELEASE" and actor_type is not ActorType.USER:
        raise ControlError("only a user can release a kill switch or resume a strategy")
    row = ControlEventRow(
        at=at,
        control=control,
        target=target,
        action=action,
        actor_type=actor_type.value,
        actor_id=actor_id,
        reason=reason[:500],
        detail=detail or {},
    )
    session.add(row)
    session.flush()
    audit.append(
        session,
        event_type=f"risk.{control.lower()}_{action.lower()}",
        actor_type=actor_type,
        actor_id=actor_id,
        subject_type="control",
        subject_id=target,
        payload={"reason": row.reason, "detail": row.detail},
    )
    return row


def set_kill_switch(
    session: Session,
    audit: AuditLog,
    *,
    active: bool,
    actor_type: ActorType,
    actor_id: str,
    reason: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Returns False if the switch was already in the requested state (nothing recorded)."""
    if control_state(session).kill_switch_active == active:
        return False
    _record(
        session,
        audit,
        control=KILL,
        target=GLOBAL,
        action="ACTIVATE" if active else "RELEASE",
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        at=at,
        detail=detail,
    )
    return True


def set_strategy_paused(
    session: Session,
    audit: AuditLog,
    *,
    strategy: str,
    paused: bool,
    actor_type: ActorType,
    actor_id: str,
    reason: str,
    at: datetime,
    detail: dict[str, Any] | None = None,
) -> bool:
    if (strategy in control_state(session).paused) == paused:
        return False
    _record(
        session,
        audit,
        control=PAUSE,
        target=strategy,
        action="ACTIVATE" if paused else "RELEASE",
        actor_type=actor_type,
        actor_id=actor_id,
        reason=reason,
        at=at,
        detail=detail,
    )
    return True


# ---------------------------------------------------------------------------- policies
def active_policy(session: Session, at: datetime) -> RiskPolicy:
    row = session.execute(
        select(RiskPolicyRow)
        .where(RiskPolicyRow.effective_from <= at)
        .order_by(RiskPolicyRow.effective_from.desc(), RiskPolicyRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return DEFAULT_POLICY
    return RiskPolicy.from_params(row.version, row.params)


def list_policies(session: Session) -> list[RiskPolicyRow]:
    return list(session.execute(select(RiskPolicyRow).order_by(RiskPolicyRow.id.desc())).scalars())


def create_policy(
    session: Session,
    audit: AuditLog,
    *,
    params: dict[str, Any],
    approved_by: str,
    reason: str,
    at: datetime,
) -> RiskPolicy:
    """Validate and store a new policy version, effective immediately. Never edits old ones."""
    if not reason.strip():
        raise ControlError("a reason is required")
    count = session.execute(select(func.count()).select_from(RiskPolicyRow)).scalar_one()
    base = active_policy(session, at)
    merged = base.to_params() | params
    policy = RiskPolicy.from_params(f"paper-{count + 2}", merged)
    session.add(
        RiskPolicyRow(
            version=policy.version,
            params=policy.to_params(),
            effective_from=at,
            approved_by=approved_by,
            reason=reason.strip()[:500],
        )
    )
    session.flush()
    audit.append(
        session,
        event_type="risk.policy_created",
        actor_type=ActorType.USER,
        actor_id=approved_by,
        subject_type="risk_policy",
        subject_id=policy.version,
        payload={"previous": base.version, "params": policy.to_params(), "reason": reason},
    )
    return policy


# ---------------------------------------------------------------------------- decisions
def risk_check_row(
    *,
    context: str,
    account_id: int | None,
    proposal: Any,
    state: Any,
    decision: RiskDecision,
    order_id: str | None,
) -> RiskCheckRow:
    p = proposal
    return RiskCheckRow(
        context=context,
        account_id=account_id,
        horizon=p.horizon.value,
        strategy=p.strategy,
        exchange=p.key.exchange.value,
        symbol=p.key.symbol,
        side=p.side.value,
        intent=p.intent.value,
        purpose=p.purpose,
        idempotency_key=p.idempotency_key,
        decided_at=p.at,
        outcome=decision.outcome.value,
        quantity=decision.quantity,
        policy_version=decision.policy_version,
        engine_version=decision.engine_version,
        order_id=order_id,
        checks=[c.as_dict() for c in decision.checks],
        inputs=state.as_dict()
        | {"ref_price": str(p.ref_price), "stop": None if p.stop is None else str(p.stop)},
    )
