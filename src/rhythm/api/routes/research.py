"""Phase 7: Claude research service (advisory). Nothing here can create an order."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from rhythm.api.deps import SessionDep, StateDep
from rhythm.api.idempotency import IdempotencyKey, run_idempotent
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Exchange, Horizon, InstrumentKey
from rhythm.db.models import AIAssessmentRow, ResearchPacketRow, StrategySignalRow
from rhythm.events import store as event_store
from rhythm.fundamentals import store as fundamentals_store
from rhythm.research.packet import MAX_DOCUMENT_CHARS, MAX_DOCUMENTS, CandidateView, build_packet
from rhythm.research.prompts import CURRENT
from rhythm.research.schema import ALLOWED_ACTIONS, output_schema
from rhythm.research.service import ResearchService

router = APIRouter(tags=["research"])

NOT_CONFIGURED = (
    "research service not configured: set RHYTHM_FEATURES__RESEARCH_SERVICE=true and "
    "RHYTHM_RESEARCH_PROVIDER (mock for local/test, anthropic with an API key)"
)


class DocumentIn(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=MAX_DOCUMENT_CHARS)


class AssessIn(BaseModel):
    signal_id: int = Field(gt=0)
    documents: list[DocumentIn] = Field(default_factory=list, max_length=MAX_DOCUMENTS)


def _service(state: StateDep) -> ResearchService:
    if state.research is None:
        raise HTTPException(503, NOT_CONFIGURED)
    return state.research


@router.get("/v1/research/status")
def status(state: StateDep, session: SessionDep) -> dict[str, Any]:
    st = state.settings
    out: dict[str, Any] = {
        "enabled": state.research is not None,
        "provider": st.research_provider,
        "feature_flag": st.features.research_service,
        "gate_paper_entries": st.research.gate_paper_entries,
        "prompt": {"id": CURRENT.id, "sha256": CURRENT.sha256},
        "allowed_actions": list(ALLOWED_ACTIONS),
        "authority": "advisory: may confirm or downgrade rule-generated candidates; "
        "cannot create, size or send orders",
    }
    if state.research is not None:
        out |= {
            "model": state.research.provider.model,
            "is_mock": state.research.provider.is_mock,
            "max_assessments_per_cycle": st.research.max_assessments_per_cycle,
            "budget": state.research.budget(session),
        }
    return out


@router.get("/v1/research/prompts")
def prompts() -> dict[str, Any]:
    return {
        "id": CURRENT.id,
        "sha256": CURRENT.sha256,
        "system": CURRENT.system,
        "instructions": CURRENT.instructions,
        "output_schema": output_schema(),
    }


def _r4_status(risk: dict[str, Any]) -> str:
    for c in risk.get("checks", []):
        if isinstance(c, dict) and c.get("code") == "R4":
            return {"PASS": "FRESH", "FAIL": "STALE"}.get(str(c.get("status")), "UNKNOWN")
    return "UNKNOWN"


def _assess(body: AssessIn, state: StateDep, session: SessionDep) -> dict[str, Any]:
    svc = _service(state)
    sig = session.get(StrategySignalRow, body.signal_id)
    if sig is None:
        raise HTTPException(404, f"signal {body.signal_id} not found")
    key = InstrumentKey(Exchange(sig.exchange), sig.symbol)
    horizon = Horizon(sig.horizon)
    at = sig.decided_at
    day = at.astimezone(EXCHANGE_TZ).date()
    events = event_store.known_events(session, key, at, day, day + timedelta(days=60))
    # The calendar the cycle actually saw: synced before the candidate was evaluated.
    synced = (
        None
        if horizon is Horizon.INTRADAY
        else event_store.last_sync(session, key, sig.created_at) is not None
    )
    tw = sig.diagnostics.get("target_weight")
    cand = CandidateView(
        key=key,
        horizon=horizon,
        signal_type=sig.signal_type,
        at=at,
        strategy=sig.strategy,
        version=sig.version,
        ref_price=sig.ref_price,
        data_is_mock=sig.data_is_mock,
        stop=sig.stop,
        target=sig.target,
        target_weight=Decimal(str(tw)) if tw is not None else None,
        reasons=tuple(sig.reasons),
        diagnostics=sig.diagnostics,
        risk=sig.risk,
        quote_status=_r4_status(sig.risk),
    )
    packet = build_packet(
        cand,
        fundamentals=fundamentals_store.known(session, key, at),
        events=events,
        documents=[(d.source, d.text) for d in body.documents],
        event_calendar_synced=synced,
    )
    out = svc.assess(
        session,
        packet,
        signal_type=sig.signal_type,
        context=sig.context,
        actor_id=state.settings.local_actor_id,
        signal_id=sig.id,
    )
    return out.as_dict() | {
        "signal_id": sig.id,
        "is_mock": svc.provider.is_mock,
        "note": "advisory only: no order was created or changed",
    }


@router.post("/v1/research/assessments", status_code=201)
def assess(
    body: AssessIn, state: StateDep, session: SessionDep, idem: IdempotencyKey = None
) -> Any:
    """Assess a stored candidate. Requires an Idempotency-Key (a retry is not re-billed)."""
    _service(state)
    return run_idempotent(
        session,
        key=idem,
        endpoint="POST /v1/research/assessments",
        body=body.model_dump(),
        produce=lambda: _assess(body, state, session),
        required=True,
        status_code=201,
    )


def _row(a: AIAssessmentRow) -> dict[str, Any]:
    return {
        "id": a.id,
        "created_at": a.created_at,
        "context": a.context,
        "signal_id": a.signal_id,
        "instrument": f"{a.exchange}:{a.symbol}",
        "horizon": a.horizon,
        "signal_type": a.signal_type,
        "provider": a.provider,
        "model": a.model,
        "is_mock": a.is_mock,
        "prompt_version": a.prompt_version,
        "status": a.status,
        "suggested_action": a.suggested_action,
        "gate": a.gate,
        "errors": a.errors,
        "usage": {
            "input_tokens": a.input_tokens,
            "output_tokens": a.output_tokens,
            "estimated": a.tokens_estimated,
            "cost_usd": None if a.cost_usd is None else str(a.cost_usd),
            "latency_ms": a.latency_ms,
            "attempts": a.attempts,
        },
    }


@router.get("/v1/research/assessments")
def list_assessments(
    session: SessionDep,
    symbol: str | None = None,
    signal_id: int | None = None,
    status: Literal["VALID", "INVALID", "TIMEOUT", "ERROR", "BUDGET_EXCEEDED", "CYCLE_LIMIT"]
    | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    stmt = select(AIAssessmentRow).order_by(AIAssessmentRow.id.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(AIAssessmentRow.symbol == symbol.split(":")[-1].upper())
    if signal_id is not None:
        stmt = stmt.where(AIAssessmentRow.signal_id == signal_id)
    if status:
        stmt = stmt.where(AIAssessmentRow.status == status)
    return [_row(a) | {"output": a.output} for a in session.execute(stmt).scalars()]


@router.get("/v1/research/assessments/{assessment_id}")
def get_assessment(assessment_id: int, session: SessionDep) -> dict[str, Any]:
    a = session.get(AIAssessmentRow, assessment_id)
    if a is None:
        raise HTTPException(404, f"assessment {assessment_id} not found")
    p = session.get(ResearchPacketRow, a.packet_id)
    return _row(a) | {
        "output": a.output,
        "raw_excerpt": a.raw_excerpt,
        "packet": None if p is None else {"id": p.id, "sha256": p.sha256, "content": p.packet},
    }
