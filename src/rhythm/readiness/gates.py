"""Live-readiness gates (Phase 10 — review only).

Each gate is MET, NOT_MET or OWNER (needs a human/external confirmation the system cannot
verify). The verdict is READY only if every blocking gate is MET — and even then nothing
changes: LIVE stays compiled out (``LIVE_TRADING_IMPLEMENTED = False``) until a separate,
explicit owner decision and a code change reviewed outside this phase.

Regulatory items cite SEBI circular SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025,
applicable from 1 Aug 2025), read from sebi.gov.in on 2026-11-04. Exchange implementation
standards (including the orders-per-second threshold) could not be retrieved and are
therefore OWNER items, never assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rhythm.audit import AuditLog
from rhythm.config.settings import LIVE_TRADING_IMPLEMENTED, Settings
from rhythm.core.clock import EXCHANGE_TZ
from rhythm.db.models import (
    AuditEvent,
    BrokerObservationRow,
    PaperAccount,
    ReconciliationRow,
    RiskPolicyRow,
    UserRow,
)
from rhythm.execution.costs import CostModel
from rhythm.monitoring.health import leader_heartbeat_age

SEBI_CIRCULAR = "SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013 (4 Feb 2025; applicable 1 Aug 2025)"
MIN_REAL_PAPER_DAYS = 60  # extended PAPER evaluation on real data before any live review
MET, NOT_MET, OWNER = "MET", "NOT_MET", "OWNER"


@dataclass(frozen=True)
class Gate:
    id: str
    area: str
    title: str
    status: str
    evidence: str
    blocking: bool = True
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "area": self.area,
            "title": self.title,
            "status": self.status,
            "evidence": self.evidence,
            "blocking": self.blocking,
            "source": self.source,
        }


@dataclass
class Review:
    generated_at: datetime
    gates: list[Gate] = field(default_factory=list)

    @property
    def blockers(self) -> list[Gate]:
        return [g for g in self.gates if g.blocking and g.status != MET]

    @property
    def verdict(self) -> str:
        return "READY_FOR_OWNER_DECISION" if not self.blockers else "NOT_READY"

    def as_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for g in self.gates if g.status == s) for s in (MET, NOT_MET, OWNER)}
        return {
            "generated_at": self.generated_at.isoformat(),
            "verdict": self.verdict,
            "live_trading_implemented": LIVE_TRADING_IMPLEMENTED,
            "note": "Review only. Nothing here enables live trading; LIVE stays locked until a "
            "separate, explicit owner approval and a reviewed code change.",
            "counts": counts,
            "blockers": len(self.blockers),
            "gates": [g.as_dict() for g in self.gates],
        }


def _sharekhan_verified(session: Session) -> tuple[bool, bool, str]:
    """Last verification run had no errors; a stream check received prices in market hours."""
    last = session.execute(
        select(BrokerObservationRow.run_id).order_by(BrokerObservationRow.id.desc()).limit(1)
    ).scalar_one_or_none()
    outcomes: list[str] = []
    if last is not None:
        outcomes = list(
            session.execute(
                select(BrokerObservationRow.outcome).where(BrokerObservationRow.run_id == last)
            ).scalars()
        )
    reads_ok = bool(outcomes) and "ERROR" not in outcomes
    streams = session.execute(
        select(AuditEvent.payload)
        .where(AuditEvent.event_type == "broker.verification.stream")
        .order_by(AuditEvent.id.desc())
        .limit(50)
    ).scalars()
    streamed = any(
        p.get("connected") and (p.get("messages") or 0) > 0 and p.get("market_open")
        for p in streams
    )
    if last is None:
        reads = "read checks not run yet (needs your keys and a Sharekhan sign-in)"
    elif reads_ok:
        reads = f"read checks passed (run {last}, {len(outcomes)} checks)"
    else:
        reads = f"read checks had errors (run {last})"
    stream = "stream check passed" if streamed else "stream check not passed in market hours"
    return reads_ok, streamed, f"{reads}; {stream}"


def _real_paper_days(session: Session) -> int:
    rows = session.execute(
        select(AuditEvent.occurred_at, AuditEvent.payload).where(
            AuditEvent.event_type == "paper.cycle"
        )
    ).all()
    days = {t.astimezone(EXCHANGE_TZ).date() for t, p in rows if p.get("data_is_mock") is False}
    return len(days)


def evaluate(session: Session, settings: Settings, now: datetime) -> Review:
    r = Review(now)
    g = r.gates.append

    # ------------------------------------------------------------------ system (automated)
    g(
        Gate(
            "SYS-01",
            "System",
            "Live order path is locked in code",
            MET if not LIVE_TRADING_IMPLEMENTED else NOT_MET,
            f"LIVE_TRADING_IMPLEMENTED={LIVE_TRADING_IMPLEMENTED}; TRADING_MODE=LIVE is refused at start-up",
            blocking=False,
        )
    )
    g(
        Gate(
            "SYS-02",
            "System",
            "A live broker order adapter exists, is reviewed and tested",
            NOT_MET,
            "No broker order method exists (by design before this review). Building it is a "
            "separate, owner-approved project after the gates below are met.",
        )
    )
    ver = AuditLog.verify(session)
    g(
        Gate(
            "SYS-03",
            "System",
            "Audit hash chain intact",
            MET if ver.ok else NOT_MET,
            f"{ver.rows_checked} rows checked"
            + ("" if ver.ok else f"; first bad row {ver.first_bad_id}"),
        )
    )
    owners = session.execute(
        select(func.count())
        .select_from(UserRow)
        .where(UserRow.role == "owner", UserRow.disabled_at.is_(None))
    ).scalar_one()
    g(
        Gate(
            "SEC-01",
            "Security",
            "Sign-in with password + authenticator; at least one active owner",
            MET
            if settings.auth.enabled and settings.auth.mfa_required and owners >= 1
            else NOT_MET,
            f"auth enabled={settings.auth.enabled}; authenticator required="
            f"{settings.auth.mfa_required}; active owners={owners}",
        )
    )
    g(
        Gate(
            "SEC-02",
            "Security",
            "Hosted behind TLS with a reviewed internet threat model",
            OWNER,
            "Local-only today. Needs the AWS deployment (TLS, private DB, secrets manager) and a review.",
        )
    )
    age = leader_heartbeat_age(session, now)
    stale = settings.scheduler.heartbeat_stale_seconds
    g(
        Gate(
            "OPS-01",
            "Operations",
            "Scheduler running with a fresh heartbeat",
            MET if age is not None and age <= stale else NOT_MET,
            f"leader heartbeat age={None if age is None else round(age)} s (stale after {stale} s)",
        )
    )
    g(
        Gate(
            "OPS-02",
            "Operations",
            "Restore drill passed in the last 30 days and backups stored off-host",
            OWNER,
            "Run scripts/restore_drill.sh (OPERATIONS_RUNBOOK); confirm off-host copies.",
        )
    )
    breaks = 0
    for (aid,) in session.execute(select(PaperAccount.id)).all():
        last = session.execute(
            select(ReconciliationRow.status)
            .where(ReconciliationRow.account_id == aid)
            .order_by(ReconciliationRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        breaks += last == "BREAK"
    g(
        Gate(
            "OPS-03",
            "Operations",
            "Every PAPER account reconciles cleanly",
            MET if breaks == 0 else NOT_MET,
            f"accounts with an open break: {breaks}",
        )
    )

    # ------------------------------------------------------------------ data & evidence
    real_market = settings.market_data_provider not in ("none", "mock")
    g(
        Gate(
            "DATA-01",
            "Data",
            "Real, licensed market data connected (not MOCK)",
            MET if real_market else NOT_MET,
            f"market_data_provider={settings.market_data_provider}",
            source="KNOWN_LIMITATIONS B-06, D-03",
        )
    )
    g(
        Gate(
            "DATA-02",
            "Data",
            "Licensed fundamentals, corporate actions and event calendar (not MOCK)",
            MET
            if settings.fundamentals_provider not in ("none", "mock")
            and settings.event_provider not in ("none", "mock")
            else NOT_MET,
            f"fundamentals={settings.fundamentals_provider}, events={settings.event_provider}",
            source="B-06, D-03",
        )
    )
    reads_ok, streamed, data03 = _sharekhan_verified(session)
    g(
        Gate(
            "DATA-03",
            "Data",
            "Sharekhan read-only verification done (login, funds, holdings, positions, history, streaming)",
            MET if reads_ok and streamed else NOT_MET,
            f"broker_adapter={settings.broker_adapter}; {data03}",
            source="SHAREKHAN_INTEGRATION §12",
        )
    )
    days = _real_paper_days(session)
    g(
        Gate(
            "EVAL-01",
            "Evidence",
            f"Extended PAPER evaluation on real data (≥ {MIN_REAL_PAPER_DAYS} trading days)",
            MET if days >= MIN_REAL_PAPER_DAYS else NOT_MET,
            f"trading days with non-MOCK PAPER cycles: {days}",
        )
    )
    cm = CostModel()
    g(
        Gate(
            "EVAL-02",
            "Evidence",
            "Cost model verified against a current Sharekhan contract note",
            MET if cm.verified else NOT_MET,
            f"cost model {cm.version}, verified={cm.verified}",
        )
    )
    policies = session.execute(select(func.count()).select_from(RiskPolicyRow)).scalar_one()
    g(
        Gate(
            "RISK-01",
            "Risk",
            "Risk limits set and approved by the owner for real money (not the illustrative defaults)",
            OWNER,
            f"{policies} owner-created policy version(s); defaults are illustrative paper limits",
            source="RISK_MANAGEMENT.md",
        )
    )

    # ------------------------------------------------------------------ regulatory & broker (external)
    src = SEBI_CIRCULAR
    for gid, title, ev in (
        (
            "REG-01",
            "Broker offers API access only via OAuth with two-factor authentication",
            "Circular para I(d): OAuth-based authentication only; 2FA for API access. Confirm Sharekhan's flow complies.",
        ),
        (
            "REG-02",
            "Static IP whitelisted by the broker for this client's API key",
            "Para I(d): no open APIs; unique vendor-client API key + static IP whitelisted by the broker. "
            "Needs a fixed egress IP (e.g. AWS NAT + Elastic IP) registered with Sharekhan.",
        ),
        (
            "REG-03",
            "Orders tagged with the exchange-provided unique algo identifier",
            "Para I(b), II(b). Sharekhan's order API documents no algo-ID field (B-10/D-series): written confirmation needed.",
        ),
        (
            "REG-04",
            "Algo registration decision: below/above the exchange orders-per-second threshold",
            "Para I(c): self-developed algos register via the broker only above the threshold set by the "
            "Broker's Industry Standards Forum. The numeric threshold was NOT retrieved — confirm with Sharekhan/exchange.",
        ),
        (
            "REG-05",
            "Use limited to the owner (and family as defined); white-box logic; no RA registration needed",
            "Para I(c) family = self, spouse, dependent children/parents; para V: black-box algos need RA registration. "
            "Offering this to others would change the regulatory position — get advice.",
        ),
        (
            "REG-06",
            "Exchange implementation standards / FAQs reviewed (NSE & BSE)",
            "Para 7(a), IV(c). Not retrievable in this session; obtain the current documents.",
        ),
    ):
        g(Gate(gid, "Regulatory", title, OWNER, ev, source=src))
    for gid, title, ev in (
        (
            "BRK-01",
            "Sharekhan written answers: product types and auto square-off time (B-02)",
            "Open blocker B-02.",
        ),
        (
            "BRK-02",
            "Sharekhan written answers: real API rate limits (B-03)",
            "Public page says up to 30 order requests/s; FAQ shows a placeholder 'XX'.",
        ),
        (
            "BRK-03",
            "Sharekhan written answers: endpoint discrepancies D1-D8 and available-funds formula (B-05, B-08)",
            "Open blockers B-05, B-08.",
        ),
        (
            "BRK-04",
            "API and market-data terms of use reviewed for storage and automated use",
            "COMPLIANCE_CHECKLIST §3.",
        ),
    ):
        g(Gate(gid, "Broker", title, OWNER, ev, source="KNOWN_LIMITATIONS, COMPLIANCE_CHECKLIST"))
    g(
        Gate(
            "OWN-01",
            "Owner",
            "Independent legal/tax advice on automated trading of your own account",
            OWNER,
            "This review is engineering, not legal advice.",
            source="COMPLIANCE_CHECKLIST",
        )
    )
    g(
        Gate(
            "OWN-02",
            "Owner",
            "Explicit written decision to build and enable LIVE, with limits and a start date",
            OWNER,
            "Required by the blueprint before any live code is written.",
        )
    )
    return r


def lead_time(now: datetime) -> datetime:
    """Earliest date the evidence gate could be met if real-data PAPER started today."""
    return now + timedelta(days=int(MIN_REAL_PAPER_DAYS * 7 / 5) + 1)


def to_markdown(review: Review) -> str:
    d = review.as_dict()
    out = [
        f"# Live-readiness review — {d['verdict']}",
        "",
        f"Generated {d['generated_at']}. {d['note']}",
        "",
        f"MET {d['counts']['MET']} · NOT_MET {d['counts']['NOT_MET']} · OWNER {d['counts']['OWNER']} "
        f"· blocking items open: {d['blockers']}",
        "",
        "| Gate | Area | Requirement | Status | Evidence |",
        "|---|---|---|---|---|",
    ]
    for gt in review.gates:
        ev = gt.evidence.replace("|", "\\|")
        out.append(f"| {gt.id} | {gt.area} | {gt.title} | {gt.status} | {ev} |")
    return "\n".join(out) + "\n"
