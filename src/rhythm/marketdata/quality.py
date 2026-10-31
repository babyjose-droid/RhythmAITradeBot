"""Recording of data-quality events (stale feeds, invalid bars, rejected snapshots...)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from rhythm.core.clock import Clock
from rhythm.core.logging import get_logger
from rhythm.core.market import InstrumentKey
from rhythm.core.redaction import redact
from rhythm.db.models import DataQualityEvent

log = get_logger(__name__)


class QualityKind(StrEnum):
    STALE = "STALE"
    MISSING = "MISSING"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    INVALID_BAR = "INVALID_BAR"
    INVALID_QUOTE = "INVALID_QUOTE"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    SNAPSHOT_REJECTED = "SNAPSHOT_REJECTED"
    RECOVERED = "RECOVERED"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


def record_quality_event(
    session: Session,
    clock: Clock,
    *,
    source: str,
    kind: QualityKind,
    severity: Severity,
    key: InstrumentKey | None = None,
    exchange: str | None = None,
    detail: dict[str, Any] | None = None,
) -> DataQualityEvent:
    event = DataQualityEvent(
        occurred_at=clock.now(),
        source=source,
        kind=kind.value,
        severity=severity.value,
        exchange=key.exchange.value if key else exchange,
        symbol=key.symbol if key else None,
        detail=redact(detail or {}),
    )
    session.add(event)
    log.warning(
        "data_quality_event",
        kind=kind.value,
        severity=severity.value,
        source=source,
        instrument=str(key) if key else None,
    )
    return event
