from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from rhythm.audit.service import GENESIS_HASH, canonical_json, compute_row_hash

BASE: dict[str, Any] = {
    "prev_hash": GENESIS_HASH,
    "occurred_at": datetime(2026, 10, 31, 4, 0, tzinfo=UTC),
    "environment": "test",
    "event_type": "system.started",
    "actor_type": "system",
    "actor_id": "api",
    "subject_type": None,
    "subject_id": None,
    "payload": {"b": 1, "a": [1, 2]},
}


def test_hash_is_deterministic_and_hex64() -> None:
    h1 = compute_row_hash(**BASE)
    assert h1 == compute_row_hash(**BASE)
    assert len(h1) == 64
    int(h1, 16)


def test_payload_key_order_does_not_matter() -> None:
    assert compute_row_hash(**BASE) == compute_row_hash(
        **{**BASE, "payload": {"a": [1, 2], "b": 1}}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prev_hash", "f" * 64),
        ("occurred_at", datetime(2026, 10, 31, 4, 0, 0, 1, tzinfo=UTC)),
        ("environment", "production"),
        ("event_type", "system.stopped"),
        ("actor_type", "user"),
        ("actor_id", "owner"),
        ("subject_type", "order"),
        ("subject_id", "1"),
        ("payload", {"b": 2, "a": [1, 2]}),
    ],
)
def test_any_field_change_changes_hash(field: str, value: Any) -> None:
    assert compute_row_hash(**BASE) != compute_row_hash(**{**BASE, field: value})


def test_canonical_json_is_compact_and_sorted() -> None:
    assert canonical_json({"b": 1, "a": "₹"}) == '{"a":"₹","b":1}'
