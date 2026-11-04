"""Readiness checks. Each check is independent and fails closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from rhythm.config import TradingMode
from rhythm.core.clock import Clock, ensure_utc
from rhythm.db.migrate import current_revision, head_revision


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)


def check_database_and_clock(engine: Engine, clock: Clock, max_skew_s: float) -> list[CheckResult]:
    try:
        with engine.connect() as conn:
            db_now = conn.execute(text("SELECT now()")).scalar_one()
    except Exception as exc:
        reason = type(exc).__name__
        return [
            CheckResult("database", False, {"error": reason}),
            CheckResult("clock_skew", False, {"error": "database unavailable"}),
        ]
    skew = abs((ensure_utc(clock.now()) - ensure_utc(db_now)).total_seconds())
    return [
        CheckResult("database", True),
        CheckResult(
            "clock_skew",
            skew <= max_skew_s,
            {"skew_seconds": round(skew, 3), "max_seconds": max_skew_s},
        ),
    ]


def check_migrations(engine: Engine) -> CheckResult:
    try:
        current = current_revision(engine)
        head = head_revision()
    except Exception as exc:
        return CheckResult("migrations", False, {"error": type(exc).__name__})
    return CheckResult(
        "migrations", current == head and head is not None, {"current": current, "head": head}
    )


def check_scheduler(engine: Engine, clock: Clock, stale_after: int) -> CheckResult:
    """Only when a scheduler is expected: its leader must have beaten recently."""
    try:
        with engine.connect() as conn:
            beat = conn.execute(
                text("SELECT max(last_beat_at) FROM scheduler_heartbeats WHERE is_leader")
            ).scalar_one_or_none()
    except Exception as exc:
        return CheckResult("scheduler", False, {"error": type(exc).__name__})
    if beat is None:
        return CheckResult("scheduler", False, {"error": "no leader heartbeat yet"})
    age = (ensure_utc(clock.now()) - ensure_utc(beat)).total_seconds()
    return CheckResult(
        "scheduler",
        age <= stale_after,
        {"heartbeat_age_seconds": round(age, 1), "stale_after": stale_after},
    )


def check_trading_mode(mode: TradingMode) -> CheckResult:
    return CheckResult("trading_mode", mode is not TradingMode.LIVE, {"mode": mode.value})
