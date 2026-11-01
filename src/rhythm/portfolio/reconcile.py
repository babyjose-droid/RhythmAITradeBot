"""Reconciliation logic (MULTI_HORIZON_PORTFOLIO.md §2, §5). Pure functions, no I/O.

For a PAPER account the source of truth is the append-only fill journal: positions, cash and
order fill quantities must all be derivable from it. ``reconcile_with_venue`` compares the
consolidated ledgers with a broker/venue position list; it is ready for a broker adapter but
is only exercised with mock data until live trading is separately approved.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from rhythm.core.market import InstrumentKey

CASH_TOLERANCE = Decimal("0.01")


@dataclass(frozen=True)
class FillFact:
    horizon: str
    key: InstrumentKey
    side: str  # BUY | SELL
    quantity: int
    price: Decimal
    charges: Decimal
    order_id: str


@dataclass(frozen=True)
class PositionFact:
    horizon: str
    key: InstrumentKey
    quantity: int


@dataclass(frozen=True)
class OrderFact:
    order_id: str
    quantity: int
    filled_quantity: int


@dataclass(frozen=True)
class Diff:
    kind: str
    subject: str
    expected: str
    actual: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReconciliationResult:
    diffs: tuple[Diff, ...]
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not self.diffs


def reconcile_paper(
    *,
    starting_cash: Decimal,
    cash: Decimal,
    positions: Iterable[PositionFact],
    fills: Iterable[FillFact],
    orders: Iterable[OrderFact],
) -> ReconciliationResult:
    fills = list(fills)
    positions = list(positions)
    orders = list(orders)
    diffs: list[Diff] = []

    journal: dict[tuple[str, InstrumentKey], int] = defaultdict(int)
    expected_cash = starting_cash
    per_order: dict[str, int] = defaultdict(int)
    for f in fills:
        signed = f.quantity if f.side == "BUY" else -f.quantity
        journal[(f.horizon, f.key)] += signed
        value = f.price * f.quantity
        expected_cash += -value if f.side == "BUY" else value
        expected_cash -= f.charges
        per_order[f.order_id] += f.quantity

    ledger = {(p.horizon, p.key): p.quantity for p in positions}
    for hk in sorted(set(journal) | set(ledger), key=lambda x: (x[0], str(x[1]))):
        exp, act = journal.get(hk, 0), ledger.get(hk, 0)
        subject = f"{hk[0]}:{hk[1]}"
        if exp < 0:
            diffs.append(Diff("NEGATIVE_POSITION", subject, ">= 0", str(exp)))
        if exp != act:
            diffs.append(Diff("POSITION_MISMATCH", subject, str(exp), str(act)))

    if abs(expected_cash - cash) > CASH_TOLERANCE:
        diffs.append(Diff("CASH_MISMATCH", "account", f"{expected_cash:.4f}", f"{cash:.4f}"))

    for o in orders:
        filled = per_order.get(o.order_id, 0)
        if filled != o.filled_quantity or o.filled_quantity > o.quantity:
            diffs.append(
                Diff(
                    "ORDER_FILL_MISMATCH",
                    o.order_id,
                    f"{filled} filled (<= {o.quantity})",
                    str(o.filled_quantity),
                )
            )
    known = {o.order_id for o in orders}
    for oid in sorted(set(per_order) - known):
        diffs.append(Diff("ORPHAN_FILL", oid, "order exists", "missing"))

    summary = {
        "fills": len(fills),
        "positions": sum(1 for q in ledger.values() if q),
        "orders_checked": len(orders),
        "expected_cash": f"{expected_cash:.4f}",
        "cash": f"{cash:.4f}",
    }
    return ReconciliationResult(tuple(diffs), summary)


def consolidate(positions: Iterable[PositionFact]) -> dict[InstrumentKey, int]:
    out: dict[InstrumentKey, int] = defaultdict(int)
    for p in positions:
        out[p.key] += p.quantity
    return {k: q for k, q in out.items() if q}


def reconcile_with_venue(
    ledgers: Iterable[PositionFact], venue_positions: Mapping[InstrumentKey, int]
) -> ReconciliationResult:
    """Invariant 1: the sum of ledger quantities per instrument equals the venue quantity."""
    cons = consolidate(ledgers)
    diffs = [
        Diff("VENUE_POSITION_MISMATCH", str(k), str(cons.get(k, 0)), str(venue_positions.get(k, 0)))
        for k in sorted(set(cons) | {k for k, q in venue_positions.items() if q})
        if cons.get(k, 0) != venue_positions.get(k, 0)
    ]
    return ReconciliationResult(tuple(diffs), {"instruments": len(cons)})
