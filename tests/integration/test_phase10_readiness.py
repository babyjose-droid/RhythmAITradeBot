"""Phase 10: the live-readiness review evaluates gates from real system state, reports
NOT_READY honestly, and never changes anything (LIVE stays locked)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from rhythm.audit import ActorType
from rhythm.cli import main
from rhythm.config import get_settings
from rhythm.config.settings import LIVE_TRADING_IMPLEMENTED
from rhythm.db.models import AuditEvent
from rhythm.db.session import session_scope
from rhythm.readiness import gates
from rhythm.scheduler.service import Scheduler
from tests.integration.test_phase9_api import Env9

pytestmark = pytest.mark.integration


def _status(review: dict[str, Any]) -> dict[str, str]:
    return {g["id"]: g["status"] for g in review["gates"]}


def test_review_reports_not_ready_on_mock_data(db_url: str, migrated: Engine) -> None:
    e = Env9(db_url, migrated)
    with e.c as c:
        e.seed()
        r = c.get("/v1/system/live-readiness").json()
        st = _status(r)
        assert r["verdict"] == "NOT_READY" and r["live_trading_implemented"] is False
        assert st["SYS-01"] == "MET" and st["SYS-02"] == "NOT_MET" and st["SYS-03"] == "MET"
        assert st["DATA-01"] == st["DATA-02"] == st["DATA-03"] == "NOT_MET"  # MOCK sources
        assert st["EVAL-01"] == "NOT_MET" and st["EVAL-02"] == "NOT_MET"
        assert st["SEC-01"] == "NOT_MET"  # auth off in this test environment
        assert st["OPS-01"] == "NOT_MET"  # no scheduler running
        assert all(st[k] == "OWNER" for k in ("REG-01", "REG-02", "REG-03", "REG-04", "OWN-02"))
        reg = next(g for g in r["gates"] if g["id"] == "REG-02")
        assert "static IP" in reg["evidence"] and "0000013" in reg["source"]
        assert "Review only" in r["note"] and r["min_real_paper_days"] == gates.MIN_REAL_PAPER_DAYS
        Scheduler(e.state, instance="rv").tick()
        assert _status(c.get("/v1/system/live-readiness").json())["OPS-01"] == "MET"
        # reviewing changed nothing: mode, LIVE flag and audit chain are as before
        assert c.get("/v1/system/mode").json()["trading_mode"] == "PAPER"
        assert LIVE_TRADING_IMPLEMENTED is False
        assert c.get("/v1/audit/verify").json()["ok"] is True


def test_evidence_gate_counts_real_paper_days_only(db_url: str, migrated: Engine) -> None:
    e = Env9(db_url, migrated)
    with e.c:
        start = e.clock.now()
        with session_scope(e.state.session_factory) as s:
            for i in range(gates.MIN_REAL_PAPER_DAYS + 5):
                e.clock.set(start + timedelta(days=i))
                e.state.audit.append(
                    s,
                    event_type="paper.cycle",
                    actor_type=ActorType.SYSTEM,
                    actor_id="t",
                    payload={"data_is_mock": i >= gates.MIN_REAL_PAPER_DAYS},  # last 5 are MOCK
                )
        with session_scope(e.state.session_factory) as s:
            st = {g.id: g for g in gates.evaluate(s, e.state.settings, e.clock.now()).gates}
            assert (
                st["EVAL-01"].status == "MET"
                and f"{gates.MIN_REAL_PAPER_DAYS}" in st["EVAL-01"].evidence
            )
            assert s.execute(
                select(AuditEvent).where(AuditEvent.event_type == "paper.cycle")
            ).first()


def test_verdict_needs_every_blocking_gate() -> None:

    r = gates.Review(
        datetime.now(UTC),
        [gates.Gate("A", "x", "a", "MET", ""), gates.Gate("B", "x", "b", "OWNER", "")],
    )
    assert r.verdict == "NOT_READY" and [g.id for g in r.blockers] == ["B"]
    r.gates[1] = gates.Gate("B", "x", "b", "OWNER", "", blocking=False)
    assert r.verdict == "READY_FOR_OWNER_DECISION"
    md = gates.to_markdown(r)
    assert (
        md.startswith("# Live-readiness review — READY_FOR_OWNER_DECISION")
        and "| A | x | a | MET |" in md
    )


def test_cli_live_readiness_exits_nonzero_when_not_ready(
    db_url: str,
    migrated: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for k, v in {
        "RHYTHM_DATABASE_URL": db_url,
        "RHYTHM_ENVIRONMENT": "test",
        "RHYTHM_AUTH__ENABLED": "false",
        "RHYTHM_LOG_LEVEL": "ERROR",
    }.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        assert main(["live-readiness", "--markdown"]) == 1
        out = capsys.readouterr().out
        assert (
            out.startswith("# Live-readiness review — NOT_READY")
            and "| REG-02 | Regulatory |" in out
        )
    finally:
        get_settings.cache_clear()
