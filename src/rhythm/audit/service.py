"""Audit log service: redacted, hash-chained, append-only.

Each row stores ``prev_hash`` (the previous row's ``row_hash``, or 64 zeros for the
first row) and ``row_hash = sha256(canonical_json(fields + prev_hash))``. Any edit,
deletion or reordering breaks the chain and is detected by :meth:`AuditLog.verify`.
Appends are serialised with a transaction-scoped advisory lock so concurrent writers
cannot fork the chain.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from rhythm.core.clock import Clock, SystemClock, ensure_utc
from rhythm.core.errors import AuditIntegrityError
from rhythm.core.redaction import redact
from rhythm.db.models import AuditEvent

GENESIS_HASH = "0" * 64
_ADVISORY_LOCK_KEY = 7_420_001  # arbitrary, constant: "audit chain" lock
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_MAX_PAYLOAD_BYTES = 64 * 1024


class ActorType(StrEnum):
    SYSTEM = "system"
    USER = "user"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_row_hash(
    *,
    prev_hash: str,
    occurred_at: datetime,
    environment: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    subject_type: str | None,
    subject_id: str | None,
    payload: dict[str, Any],
) -> str:
    material = canonical_json(
        {
            "prev_hash": prev_hash,
            "occurred_at": ensure_utc(occurred_at).isoformat(timespec="microseconds"),
            "environment": environment,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "payload": payload,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    rows_checked: int
    first_bad_id: int | None = None
    reason: str | None = None


class AuditLog:
    def __init__(self, *, environment: str, clock: Clock | None = None) -> None:
        self._environment = environment
        self._clock = clock or SystemClock()

    def append(
        self,
        session: Session,
        *,
        event_type: str,
        actor_type: ActorType,
        actor_id: str,
        payload: dict[str, Any] | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> AuditEvent:
        """Append an event inside the caller's transaction (commit is the caller's job)."""
        if not _EVENT_TYPE.match(event_type):
            raise ValueError(f"invalid event_type {event_type!r}; use dotted lower_snake form")
        if not actor_id:
            raise ValueError("actor_id is required")

        safe_payload: dict[str, Any] = redact(payload or {})
        encoded = canonical_json(safe_payload)  # also proves JSON-serialisable
        if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValueError("audit payload too large")
        # Round-trip so the stored JSONB and the hashed value are identical.
        safe_payload = json.loads(encoded)

        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        prev = session.execute(
            select(AuditEvent.row_hash).order_by(AuditEvent.id.desc()).limit(1)
        ).scalar_one_or_none()
        prev_hash = prev or GENESIS_HASH
        occurred_at = ensure_utc(self._clock.now())

        row_hash = compute_row_hash(
            prev_hash=prev_hash,
            occurred_at=occurred_at,
            environment=self._environment,
            event_type=event_type,
            actor_type=actor_type.value,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=safe_payload,
        )
        event = AuditEvent(
            occurred_at=occurred_at,
            environment=self._environment,
            event_type=event_type,
            actor_type=actor_type.value,
            actor_id=actor_id,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=safe_payload,
            prev_hash=prev_hash,
            row_hash=row_hash,
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def verify(session: Session, *, raise_on_failure: bool = False) -> VerificationResult:
        expected_prev = GENESIS_HASH
        count = 0
        rows = session.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars()
        for row in rows:
            count += 1
            reason: str | None = None
            if row.prev_hash != expected_prev:
                reason = "prev_hash does not match previous row"
            else:
                recomputed = compute_row_hash(
                    prev_hash=row.prev_hash,
                    occurred_at=row.occurred_at,
                    environment=row.environment,
                    event_type=row.event_type,
                    actor_type=row.actor_type,
                    actor_id=row.actor_id,
                    subject_type=row.subject_type,
                    subject_id=row.subject_id,
                    payload=row.payload,
                )
                if recomputed != row.row_hash:
                    reason = "row_hash mismatch (row content altered)"
            if reason:
                result = VerificationResult(False, count, row.id, reason)
                if raise_on_failure:
                    raise AuditIntegrityError(f"audit row {row.id}: {reason}")
                return result
            expected_prev = row.row_hash
        return VerificationResult(True, count)
