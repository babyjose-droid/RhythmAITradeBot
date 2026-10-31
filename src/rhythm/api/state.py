"""Process-wide application state, attached to ``app.state.rhythm``."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from rhythm.audit import AuditLog
from rhythm.config import Settings
from rhythm.core.clock import Clock
from rhythm.core.logging import get_logger
from rhythm.db.models import SystemHealthEvent
from rhythm.db.session import session_scope

log = get_logger(__name__)


@dataclass
class AppState:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    clock: Clock
    audit: AuditLog
    _last_ready: bool | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_readiness(self, ok: bool, checks: dict[str, bool]) -> None:
        """Persist a health event only when readiness changes (avoids log spam)."""
        with self._lock:
            if self._last_ready == ok:
                return
            self._last_ready = ok
        log.info("readiness_changed", ready=ok, checks=checks)
        if not checks.get("database", False):
            return  # cannot persist without a database; the log line above is the record
        try:
            with session_scope(self.session_factory) as s:
                s.add(
                    SystemHealthEvent(
                        occurred_at=self.clock.now(),
                        component="api.readiness",
                        status="ok" if ok else "down",
                        detail={"checks": checks},
                    )
                )
        except Exception:
            log.exception("health_event_write_failed")
