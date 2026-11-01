"""Idempotency-Key support for mutating endpoints (API_SPECIFICATION.md conventions).

The first request with a key runs and its response is stored in the same transaction. A retry
with the same key and the same body gets the stored response (``idempotent-replay: true``)
without running anything again. The same key with a different body is rejected (409). Two
concurrent first requests: one commits, the other fails on the primary key and is rolled
back as a whole (409), so the work is never done twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rhythm.db.models import IdempotencyRecord

_KEY = re.compile(r"^[A-Za-z0-9._:\-]{8,128}$")

IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="Client-generated unique key (8-128 chars). Retries with the same key "
        "replay the first response instead of repeating the action.",
    ),
]


def _hash(body: Any) -> str:
    return hashlib.sha256(
        json.dumps(jsonable_encoder(body), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_idempotent(
    session: Session,
    *,
    key: str | None,
    endpoint: str,
    body: Any,
    produce: Callable[[], Any],
    required: bool,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Any:
    if key is None:
        if required:
            raise HTTPException(428, "Idempotency-Key header is required for this action")
        return produce()
    if not _KEY.match(key):
        raise HTTPException(422, "Idempotency-Key must be 8-128 chars of [A-Za-z0-9._:-]")
    digest = _hash(body)
    rec = session.get(IdempotencyRecord, (key, endpoint), with_for_update=True)
    if rec is not None:
        if rec.request_hash != digest:
            raise HTTPException(409, "Idempotency-Key was already used with a different request")
        return JSONResponse(
            rec.response,
            status_code=rec.status_code,
            headers=(headers or {}) | {"idempotent-replay": "true"},
        )
    data = jsonable_encoder(produce())
    session.add(
        IdempotencyRecord(
            key=key, endpoint=endpoint, request_hash=digest, status_code=status_code, response=data
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:  # concurrent first use of the same key
        raise HTTPException(409, "a request with this Idempotency-Key is in progress") from exc
    return data
