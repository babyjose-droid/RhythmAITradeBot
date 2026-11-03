"""Background webhook delivery for the API process (only when a webhook is configured)."""

from __future__ import annotations

import threading

import httpx
from sqlalchemy.orm import Session, sessionmaker

from rhythm.config.settings import NotificationSettings
from rhythm.core.clock import Clock
from rhythm.core.logging import get_logger
from rhythm.db.session import session_scope
from rhythm.notifications.service import deliver_pending

log = get_logger(__name__)


class WebhookDispatcher:
    def __init__(
        self,
        settings: NotificationSettings,
        session_factory: sessionmaker[Session],
        clock: Clock,
        environment: str,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.clock = clock
        self.environment = environment
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(follow_redirects=False)

    def run_once(self) -> dict[str, int]:
        with session_scope(self.session_factory) as s:
            return deliver_pending(
                s,
                self.settings,
                now=self.clock.now(),
                environment=self.environment,
                client=self._client,
            )

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.dispatch_interval_seconds):
            try:
                res = self.run_once()
                if res["sent"] or res["failed"]:
                    log.info("notifications_delivered", **res)
            except Exception:
                log.exception("notification_dispatch_failed")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="webhook-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._client.close()
