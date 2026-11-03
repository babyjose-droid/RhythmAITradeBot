"""Assessment service: budget, provider call, validation, gate, persistence and audit.

``assess`` never raises for provider or output problems: every failure becomes a stored
assessment with a non-VALID status, which the gate treats as "hold for review".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.config.settings import ResearchSettings
from rhythm.core.clock import EXCHANGE_TZ, Clock
from rhythm.db.models import AIAssessmentRow, PromptVersionRow, ResearchPacketRow
from rhythm.research.gate import GateOutcome, gate
from rhythm.research.packet import EvidencePacket
from rhythm.research.prompts import CURRENT, PromptVersion
from rhythm.research.providers import ProviderError, ProviderRequest, ResearchProvider
from rhythm.research.schema import Assessment
from rhythm.research.validate import validate

RAW_EXCERPT_CHARS = 4000


@dataclass
class AssessmentOutcome:
    id: int
    status: str
    suggested_action: str | None
    gate: GateOutcome
    errors: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None
    packet_id: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "suggested_action": self.suggested_action,
            "gate": self.gate.as_dict(),
            "errors": self.errors,
            "output": self.output,
            "packet_id": self.packet_id,
        }


def _day_start(now: datetime) -> datetime:
    d = now.astimezone(EXCHANGE_TZ).date()
    return datetime(d.year, d.month, d.day, tzinfo=EXCHANGE_TZ)


class ResearchService:
    def __init__(
        self,
        provider: ResearchProvider,
        settings: ResearchSettings,
        clock: Clock,
        audit: AuditLog,
        prompt: PromptVersion = CURRENT,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.clock = clock
        self.audit = audit
        self.prompt = prompt

    # ------------------------------------------------------------------ budget
    def budget(self, session: Session) -> dict[str, Any]:
        now = self.clock.now()
        start = _day_start(now)
        used, count, cost = session.execute(
            select(
                func.coalesce(
                    func.sum(AIAssessmentRow.input_tokens + AIAssessmentRow.output_tokens), 0
                ),
                func.count(AIAssessmentRow.id),
                func.sum(AIAssessmentRow.cost_usd),
            ).where(AIAssessmentRow.created_at >= start)
        ).one()
        limit = self.settings.daily_token_budget
        return {
            "day": start.date().isoformat(),
            "tokens_used": int(used),
            "token_budget": limit,
            "tokens_remaining": max(0, limit - int(used)),
            "assessments": int(count),
            "cost_usd": None if cost is None else str(cost),
            "cost_rates_configured": self.settings.input_usd_per_mtok is not None
            and self.settings.output_usd_per_mtok is not None,
            "resets_at": (start + timedelta(days=1)).isoformat(),
        }

    def _cost(self, inp: int, out: int) -> Decimal | None:
        a, b = self.settings.input_usd_per_mtok, self.settings.output_usd_per_mtok
        if a is None or b is None:
            return None
        return (Decimal(str(a)) * inp + Decimal(str(b)) * out) / Decimal(1_000_000)

    # ------------------------------------------------------------------ storage
    def _prompt_row(self, session: Session) -> None:
        existing = session.get(PromptVersionRow, self.prompt.id)
        if existing is None:
            session.add(
                PromptVersionRow(
                    id=self.prompt.id,
                    sha256=self.prompt.sha256,
                    system_text=self.prompt.system,
                    instructions=self.prompt.instructions,
                )
            )
            session.flush()
        elif existing.sha256 != self.prompt.sha256:
            raise RuntimeError(f"prompt {self.prompt.id} text changed without a new version")

    def store_packet(self, session: Session, packet: EvidencePacket) -> int:
        digest = packet.sha256
        existing = session.execute(
            select(ResearchPacketRow.id).where(ResearchPacketRow.sha256 == digest)
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = ResearchPacketRow(
            sha256=digest,
            exchange=packet.exchange,
            symbol=packet.symbol,
            horizon=packet.horizon,
            as_of=datetime.fromisoformat(packet.as_of),
            packet=packet.as_dict(),
            data_is_mock=packet.data_is_mock,
        )
        session.add(row)
        session.flush()
        return row.id

    # ------------------------------------------------------------------ assess
    def assess(
        self,
        session: Session,
        packet: EvidencePacket,
        *,
        signal_type: str,
        context: str,
        actor_id: str,
        signal_id: int | None = None,
        skip_reason: str | None = None,
    ) -> AssessmentOutcome:
        self._prompt_row(session)
        packet_id = self.store_packet(session, packet)
        user = self.prompt.render(packet)
        est_in = max(1, (len(self.prompt.system) + len(user)) // 4)
        status, text = "ERROR", ""
        errors: list[str] = []
        output: dict[str, Any] | None = None
        action: str | None = None
        inp = out = attempts = 0
        estimated, model = False, self.provider.model
        started = time.perf_counter()
        budget = self.budget(session)
        if skip_reason is not None:
            status, errors = "CYCLE_LIMIT", [skip_reason]
        elif (
            budget["tokens_used"] + est_in + self.settings.max_output_tokens
            > budget["token_budget"]
        ):
            status = "BUDGET_EXCEEDED"
            errors = [
                "daily token budget would be exceeded "
                f"({budget['tokens_used']} used of {budget['token_budget']})"
            ]
        else:
            try:
                resp = self.provider.complete(
                    ProviderRequest(self.prompt.system, user, self.settings.max_output_tokens)
                )
            except ProviderError as exc:
                status = "TIMEOUT" if exc.kind == "timeout" else "ERROR"
                errors, attempts = [f"{exc.kind}: {exc}"], exc.attempts
            else:
                text, model, attempts = resp.text, resp.model, resp.attempts
                inp, out, estimated = resp.input_tokens, resp.output_tokens, resp.tokens_estimated
                result = validate(text, packet)
                if resp.stop_reason == "max_tokens" and not result.ok:
                    result.errors.insert(0, "output truncated at max_output_tokens")
                status = "VALID" if result.ok else "INVALID"
                errors = result.errors
                if result.assessment is not None:
                    output = result.assessment.model_dump()
                    action = result.assessment.suggested_action
        latency = int((time.perf_counter() - started) * 1000)
        g = gate(signal_type, status, Assessment.model_validate(output) if output else None)
        row = AIAssessmentRow(
            created_at=self.clock.now(),
            context=context,
            packet_id=packet_id,
            signal_id=signal_id,
            exchange=packet.exchange,
            symbol=packet.symbol,
            horizon=packet.horizon,
            signal_type=signal_type,
            provider=self.provider.name,
            model=model,
            is_mock=self.provider.is_mock,
            prompt_version=self.prompt.id,
            status=status,
            suggested_action=action,
            gate=g.as_dict(),
            output=output,
            errors=errors,
            raw_excerpt=text[:RAW_EXCERPT_CHARS],
            input_tokens=inp,
            output_tokens=out,
            tokens_estimated=estimated,
            cost_usd=self._cost(inp, out),
            latency_ms=latency,
            attempts=attempts,
        )
        session.add(row)
        session.flush()
        self.audit.append(
            session,
            event_type="research.assessed",
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            subject_type="ai_assessment",
            subject_id=str(row.id),
            payload={
                "symbol": f"{packet.exchange}:{packet.symbol}",
                "context": context,
                "status": status,
                "suggested_action": action,
                "gate": g.as_dict(),
                "provider": self.provider.name,
                "is_mock": self.provider.is_mock,
                "prompt": self.prompt.id,
                "packet_sha256": packet.sha256,
                "tokens": inp + out,
            },
        )
        return AssessmentOutcome(row.id, status, action, g, errors, output, packet_id)
