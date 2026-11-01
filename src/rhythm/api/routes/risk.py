"""Risk controls, policies, risk decisions, ledgers, consolidated view and reconciliation.

Mutating endpoints require an ``Idempotency-Key`` header. Releasing the kill switch and
resuming a strategy are user actions with a mandatory reason; all changes are audited.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from rhythm.api.deps import SessionDep, StateDep
from rhythm.api.idempotency import IdempotencyKey, run_idempotent
from rhythm.audit import ActorType
from rhythm.controls import service as controls
from rhythm.core.market import Exchange, Horizon, InstrumentKey
from rhythm.db.models import RiskCheckRow
from rhythm.marketdata.provider import MarketDataUnavailableError
from rhythm.paper import service as paper
from rhythm.portfolio import service as portfolio
from rhythm.risk.engine import ENGINE_VERSION
from rhythm.risk.policy import PolicyValidationError
from rhythm.strategies.intraday_orb import STRATEGIES

router = APIRouter(tags=["risk"])
PAPER_HEADERS = {"x-account-kind": "PAPER"}


def _account(session: SessionDep, account_id: int, *, lock: bool = False) -> Any:
    try:
        return paper.get_account(session, account_id, lock=lock)
    except paper.AccountNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


# ---------------------------------------------------------------------------- controls
@router.get("/v1/risk/controls")
def get_controls(session: SessionDep, history: int = Query(20, ge=0, le=500)) -> dict[str, Any]:
    return controls.control_state(session).as_dict() | {
        "history": controls.control_history(session, history) if history else []
    }


class KillSwitchIn(BaseModel):
    action: Literal["activate", "release"]
    reason: str = Field(min_length=3, max_length=500)
    flatten_intraday: bool = False


@router.post("/v1/risk/kill-switch")
def kill_switch(
    body: KillSwitchIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    def produce() -> dict[str, Any]:
        now = state.clock.now()
        actor = state.settings.local_actor_id
        if body.action == "activate":
            res = paper.activate_kill_switch(
                session,
                state.audit,
                settings=state.settings,
                actor_type=ActorType.USER,
                actor_id=actor,
                reason=body.reason,
                at=now,
                flatten_intraday=body.flatten_intraday,
            )
            out: dict[str, Any] = {
                "changed": res.changed,
                "cancelled_orders": res.cancelled_orders,
                "flatten_orders": res.flatten_orders,
                "notes": res.notes,
            }
        else:
            if body.flatten_intraday:
                raise HTTPException(422, "flatten_intraday only applies to activation")
            changed = controls.set_kill_switch(
                session,
                state.audit,
                active=False,
                actor_type=ActorType.USER,
                actor_id=actor,
                reason=body.reason,
                at=now,
            )
            out = {"changed": changed}
        return out | controls.control_state(session).as_dict()

    return run_idempotent(
        session,
        key=idem,
        endpoint="POST /v1/risk/kill-switch",
        body=body.model_dump(),
        produce=produce,
        required=True,
    )


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _pause(
    name: str, paused: bool, body: ReasonIn, state: StateDep, session: SessionDep, idem: str | None
) -> Any:
    if name not in STRATEGIES:
        raise HTTPException(404, f"unknown strategy {name!r}")

    def produce() -> dict[str, Any]:
        changed = controls.set_strategy_paused(
            session,
            state.audit,
            strategy=name,
            paused=paused,
            actor_type=ActorType.USER,
            actor_id=state.settings.local_actor_id,
            reason=body.reason,
            at=state.clock.now(),
        )
        return {"strategy": name, "paused": paused, "changed": changed}

    return run_idempotent(
        session,
        key=idem,
        endpoint=f"POST /v1/strategies/{name}/{'pause' if paused else 'resume'}",
        body=body.model_dump(),
        produce=produce,
        required=True,
    )


@router.post("/v1/strategies/{name}/pause")
def pause_strategy(
    name: str, body: ReasonIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    return _pause(name, True, body, state, session, idem)


@router.post("/v1/strategies/{name}/resume")
def resume_strategy(
    name: str, body: ReasonIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    return _pause(name, False, body, state, session, idem)


# ---------------------------------------------------------------------------- policies
@router.get("/v1/risk/policies")
def policies(state: StateDep, session: SessionDep) -> dict[str, Any]:
    active = controls.active_policy(session, state.clock.now())
    return {
        "engine_version": ENGINE_VERSION,
        "active": active.describe(),
        "note": "Illustrative paper-test limits, not recommendations.",
        "history": [
            {
                "version": r.version,
                "effective_from": r.effective_from,
                "approved_by": r.approved_by,
                "reason": r.reason,
                "params": r.params,
            }
            for r in controls.list_policies(session)
        ],
    }


class PolicyIn(BaseModel):
    params: dict[str, Any] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500)


@router.post("/v1/risk/policies", status_code=201)
def create_policy(
    body: PolicyIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    def produce() -> dict[str, Any]:
        try:
            pol = controls.create_policy(
                session,
                state.audit,
                params=body.params,
                approved_by=state.settings.local_actor_id,
                reason=body.reason,
                at=state.clock.now(),
            )
        except (PolicyValidationError, controls.ControlError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return pol.describe()

    return run_idempotent(
        session,
        key=idem,
        endpoint="POST /v1/risk/policies",
        body=body.model_dump(),
        produce=produce,
        required=True,
        status_code=201,
    )


# ---------------------------------------------------------------------------- decisions
@router.get("/v1/risk/checks")
def risk_checks(
    session: SessionDep,
    account_id: int | None = None,
    outcome: Literal["ACCEPT", "REDUCE", "REVIEW", "BLOCK"] | None = None,
    order_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    stmt = select(RiskCheckRow).order_by(RiskCheckRow.id.desc()).limit(limit)
    if account_id is not None:
        stmt = stmt.where(RiskCheckRow.account_id == account_id)
    if outcome:
        stmt = stmt.where(RiskCheckRow.outcome == outcome)
    if order_id:
        stmt = stmt.where(RiskCheckRow.order_id == order_id)
    return [
        {
            "id": r.id,
            "context": r.context,
            "account_id": r.account_id,
            "horizon": r.horizon,
            "strategy": r.strategy,
            "instrument": f"{r.exchange}:{r.symbol}",
            "side": r.side,
            "intent": r.intent,
            "purpose": r.purpose,
            "decided_at": r.decided_at,
            "outcome": r.outcome,
            "quantity": r.quantity,
            "policy_version": r.policy_version,
            "engine_version": r.engine_version,
            "order_id": r.order_id,
            "checks": r.checks,
            "inputs": r.inputs,
        }
        for r in session.execute(stmt).scalars()
    ]


# ---------------------------------------------------------------------------- portfolio
def _marks(state: StateDep, keys: list[InstrumentKey]) -> dict[InstrumentKey, Decimal]:
    out: dict[InstrumentKey, Decimal] = {}
    if state.market_data is None:
        return out
    for k in keys:
        try:
            q = state.market_data.latest_quote(k)
        except MarketDataUnavailableError:
            continue
        if q is not None:
            out[k] = q.ltp
    return out


def _held_keys(session: SessionDep, account_id: int) -> list[InstrumentKey]:
    return sorted(
        {
            InstrumentKey(Exchange(p.exchange), p.symbol)
            for p in portfolio.positions(session, account_id)
            if p.quantity > 0
        }
    )


@router.get("/v1/portfolio/ledgers")
def ledgers(
    account_id: int, state: StateDep, session: SessionDep, response: Response
) -> dict[str, Any]:
    response.headers.update(PAPER_HEADERS)
    acct = _account(session, account_id)
    marks = _marks(state, _held_keys(session, account_id))
    return {
        "account_id": acct.id,
        "account_kind": "PAPER",
        "cash": str(acct.cash),
        "ledgers": portfolio.ledger_summaries(session, account_id, marks),
    }


@router.get("/v1/portfolio/consolidated")
def consolidated(
    account_id: int, state: StateDep, session: SessionDep, response: Response
) -> dict[str, Any]:
    response.headers.update(PAPER_HEADERS)
    acct = _account(session, account_id)
    marks = _marks(state, _held_keys(session, account_id))
    latest = portfolio.latest_reconciliation(session, account_id)
    return {
        "account_id": acct.id,
        "account_kind": "PAPER",
        "positions": portfolio.consolidated(session, account_id, marks),
        "reconciliation": None if latest is None else _recon_out(latest),
    }


class AllocationIn(BaseModel):
    allocated_capital: Decimal = Field(ge=0, le=Decimal(10**12))
    reason: str = Field(min_length=3, max_length=500)


@router.put("/v1/paper/accounts/{account_id}/ledgers/{horizon}")
def set_allocation(
    account_id: int,
    horizon: Horizon,
    body: AllocationIn,
    state: StateDep,
    session: SessionDep,
    idem: IdempotencyKey = None,
) -> Any:
    def produce() -> dict[str, Any]:
        acct = _account(session, account_id, lock=True)
        try:
            row = portfolio.set_allocation(
                session,
                state.audit,
                acct=acct,
                horizon=horizon,
                allocated_capital=body.allocated_capital,
                actor_id=state.settings.local_actor_id,
                reason=body.reason,
            )
        except portfolio.LedgerError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "account_id": account_id,
            "horizon": row.horizon,
            "allocated_capital": str(row.allocated_capital),
        }

    return run_idempotent(
        session,
        key=idem,
        endpoint=f"PUT /v1/paper/accounts/{account_id}/ledgers/{horizon.value}",
        body=body.model_dump(),
        produce=produce,
        required=True,
        headers=PAPER_HEADERS,
    )


# ---------------------------------------------------------------------------- reconciliation
def _recon_out(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "account_id": r.account_id,
        "run_at": r.run_at,
        "scope": r.scope,
        "trigger": r.trigger,
        "status": r.status,
        "diffs": r.diffs,
        "summary": r.summary,
    }


@router.post("/v1/paper/accounts/{account_id}/reconcile")
def reconcile(
    account_id: int, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    def produce() -> dict[str, Any]:
        acct = _account(session, account_id, lock=True)
        now = state.clock.now()
        result = portfolio.reconcile_account(session, acct)
        row = portfolio.record_reconciliation(
            session,
            state.audit,
            acct=acct,
            result=result,
            at=now,
            trigger="manual",
            actor_id=state.settings.local_actor_id,
        )
        if not result.ok:
            controls.set_kill_switch(
                session,
                state.audit,
                active=True,
                actor_type=ActorType.SYSTEM,
                actor_id="reconciliation",
                reason=f"reconciliation break on PAPER account {acct.id} (manual)",
                at=now,
                detail={"account_id": acct.id},
            )
        return _recon_out(row)

    return run_idempotent(
        session,
        key=idem,
        endpoint=f"POST /v1/paper/accounts/{account_id}/reconcile",
        body={},
        produce=produce,
        required=False,
        headers=PAPER_HEADERS,
    )


@router.get("/v1/reconciliations")
def reconciliations(
    account_id: int, session: SessionDep, limit: int = Query(50, ge=1, le=500)
) -> list[dict[str, Any]]:
    _account(session, account_id)
    return [_recon_out(r) for r in portfolio.list_reconciliations(session, account_id, limit)]
