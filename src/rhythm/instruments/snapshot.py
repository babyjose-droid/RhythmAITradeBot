"""Pure snapshot comparison: what changed between the registry and an incoming scrip master.

Safety rules (fail closed):
* an empty snapshot is refused (it would delist everything);
* duplicate symbols or invalid rows make the snapshot invalid;
* if more than ``max_removal_ratio`` of currently active instruments would disappear,
  the snapshot is refused for human review (likely a partial/failed download).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from rhythm.core.market import Exchange
from rhythm.instruments.types import ScripRecord

DEFAULT_MAX_REMOVAL_RATIO = 0.2


class SnapshotRejectedError(ValueError):
    def __init__(self, reason: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


@dataclass(frozen=True)
class SnapshotDiff:
    added: list[ScripRecord] = field(default_factory=list)
    changed: list[ScripRecord] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # symbols
    unchanged: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "changed": len(self.changed),
            "removed": len(self.removed),
            "unchanged": self.unchanged,
        }


def validate_records(records: Sequence[ScripRecord], exchange: Exchange) -> None:
    if not records:
        raise SnapshotRejectedError("empty snapshot")
    seen: set[str] = set()
    problems: list[str] = []
    for r in records:
        if r.exchange is not exchange:
            problems.append(f"{r.symbol}: exchange {r.exchange} != {exchange}")
        if r.symbol in seen:
            problems.append(f"{r.symbol}: duplicate symbol")
        seen.add(r.symbol)
        if r.tick_size <= Decimal(0):
            problems.append(f"{r.symbol}: non-positive tick size")
        if r.lot_size < 1:
            problems.append(f"{r.symbol}: lot size < 1")
    if problems:
        raise SnapshotRejectedError(
            "invalid rows", {"problems": problems[:50], "count": len(problems)}
        )


def content_hash(records: Sequence[ScripRecord]) -> str:
    rows = sorted(
        ({"symbol": r.symbol, **r.attributes()} for r in records), key=lambda x: str(x["symbol"])
    )
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def diff_snapshot(
    current: Mapping[str, Mapping[str, str | int | None]],
    incoming: Sequence[ScripRecord],
    exchange: Exchange,
    *,
    max_removal_ratio: float = DEFAULT_MAX_REMOVAL_RATIO,
) -> SnapshotDiff:
    """``current`` maps active symbol -> attributes (as produced by ScripRecord.attributes)."""
    validate_records(incoming, exchange)
    by_symbol = {r.symbol: r for r in incoming}
    added = [r for s, r in by_symbol.items() if s not in current]
    changed = [
        r for s, r in by_symbol.items() if s in current and dict(current[s]) != r.attributes()
    ]
    removed = sorted(s for s in current if s not in by_symbol)
    unchanged = len(by_symbol) - len(added) - len(changed)
    if current and len(removed) / len(current) > max_removal_ratio:
        raise SnapshotRejectedError(
            "too many removals; review required",
            {"removed": len(removed), "active": len(current), "max_ratio": max_removal_ratio},
        )
    return SnapshotDiff(added=added, changed=changed, removed=removed, unchanged=unchanged)
