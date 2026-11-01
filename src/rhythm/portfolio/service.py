"""Ledgers, consolidated view and persisted reconciliation for PAPER accounts."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.core.errors import RhythmError
from rhythm.core.market import Exchange, Horizon, InstrumentKey
from rhythm.db.models import (
    FillRow,
    Ledger,
    OrderRow,
    PaperAccount,
    PaperPosition,
    ReconciliationRow,
)
from rhythm.portfolio.reconcile import (
    FillFact,
    OrderFact,
    PositionFact,
    ReconciliationResult,
    reconcile_paper,
)
from rhythm.risk.policy import RiskPolicy

ACCOUNT_KIND = "PAPER"


class LedgerError(RhythmError):
    pass


def _key(exchange: str, symbol: str) -> InstrumentKey:
    return InstrumentKey(Exchange(exchange), symbol)


# ---------------------------------------------------------------------------- ledgers
def create_ledgers(session: Session, acct: PaperAccount, policy: RiskPolicy) -> list[Ledger]:
    rows = [
        Ledger(
            account_kind=ACCOUNT_KIND,
            account_id=acct.id,
            horizon=h.value,
            allocated_capital=(acct.starting_cash * policy.ledger_allocation_pct[h.value]).quantize(
                Decimal("0.0001")
            ),
        )
        for h in Horizon
    ]
    session.add_all(rows)
    session.flush()
    return rows


def ledgers(session: Session, account_id: int) -> dict[str, Ledger]:
    rows = session.execute(select(Ledger).where(Ledger.account_id == account_id)).scalars()
    return {r.horizon: r for r in rows}


def set_allocation(
    session: Session,
    audit: AuditLog,
    *,
    acct: PaperAccount,
    horizon: Horizon,
    allocated_capital: Decimal,
    actor_id: str,
    reason: str,
) -> Ledger:
    if not reason.strip():
        raise LedgerError("a reason is required")
    if allocated_capital < 0:
        raise LedgerError("allocation must not be negative")
    current = ledgers(session, acct.id)
    if horizon.value not in current:
        raise LedgerError("ledger not found")
    others = sum(
        (lg.allocated_capital for h, lg in current.items() if h != horizon.value), Decimal(0)
    )
    if others + allocated_capital > acct.starting_cash:
        raise LedgerError("total allocation would exceed the account's starting capital")
    row = current[horizon.value]
    before = row.allocated_capital
    row.allocated_capital = allocated_capital
    session.flush()
    audit.append(
        session,
        event_type="portfolio.allocation_changed",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="ledger",
        subject_id=f"{acct.id}:{horizon.value}",
        payload={"from": str(before), "to": str(allocated_capital), "reason": reason.strip()},
    )
    return row


def positions(session: Session, account_id: int) -> list[PaperPosition]:
    return list(
        session.execute(
            select(PaperPosition).where(PaperPosition.account_id == account_id)
        ).scalars()
    )


def ledger_summaries(
    session: Session, account_id: int, marks: dict[InstrumentKey, Decimal]
) -> list[dict[str, Any]]:
    by_h: dict[str, list[PaperPosition]] = defaultdict(list)
    for p in positions(session, account_id):
        by_h[p.horizon].append(p)
    out = []
    order = {h.value: i for i, h in enumerate(Horizon)}
    for h, lg in sorted(ledgers(session, account_id).items(), key=lambda x: order[x[0]]):
        held = [p for p in by_h.get(h, []) if p.quantity > 0]
        exposure = sum(
            (marks.get(_key(p.exchange, p.symbol), p.average_price) * p.quantity for p in held),
            Decimal(0),
        )
        out.append(
            {
                "horizon": h,
                "account_kind": ACCOUNT_KIND,
                "allocated_capital": str(lg.allocated_capital),
                "exposure": str(exposure.quantize(Decimal("0.01"))),
                "utilisation_pct": (
                    float(exposure / lg.allocated_capital * 100) if lg.allocated_capital else None
                ),
                "open_positions": len(held),
                "realized_pnl": str(sum((p.realized_pnl for p in by_h.get(h, [])), Decimal(0))),
                "charges_paid": str(sum((p.charges_paid for p in by_h.get(h, [])), Decimal(0))),
            }
        )
    return out


def consolidated(
    session: Session, account_id: int, marks: dict[InstrumentKey, Decimal]
) -> list[dict[str, Any]]:
    """Per instrument: total quantity across ledgers, with the per-ledger breakdown."""
    agg: dict[InstrumentKey, dict[str, Any]] = {}
    for p in positions(session, account_id):
        if p.quantity <= 0:
            continue
        k = _key(p.exchange, p.symbol)
        row = agg.setdefault(k, {"instrument": str(k), "quantity": 0, "by_ledger": {}})
        row["quantity"] += p.quantity
        row["by_ledger"][p.horizon] = {
            "quantity": p.quantity,
            "average_price": str(p.average_price),
        }
    out = []
    for k, row in sorted(agg.items()):
        mark = marks.get(k)
        row["mark"] = None if mark is None else str(mark)
        row["mark_source"] = "latest quote" if mark is not None else "unavailable (cost shown)"
        cost = sum(
            (Decimal(v["average_price"]) * v["quantity"] for v in row["by_ledger"].values()),
            Decimal(0),
        )
        row["value"] = str(
            ((mark * row["quantity"]) if mark is not None else cost).quantize(Decimal("0.01"))
        )
        out.append(row)
    return out


# ---------------------------------------------------------------------------- reconciliation
def reconcile_account(session: Session, acct: PaperAccount) -> ReconciliationResult:
    fills = [
        FillFact(
            f.horizon,
            _key(f.exchange, f.symbol),
            f.side,
            f.quantity,
            f.price,
            f.charges_total,
            f.order_id,
        )
        for f in session.execute(select(FillRow).where(FillRow.account_id == acct.id)).scalars()
    ]
    pos = [
        PositionFact(p.horizon, _key(p.exchange, p.symbol), p.quantity)
        for p in positions(session, acct.id)
    ]
    orders = [
        OrderFact(o.id, o.quantity, o.filled_quantity)
        for o in session.execute(select(OrderRow).where(OrderRow.account_id == acct.id)).scalars()
    ]
    return reconcile_paper(
        starting_cash=acct.starting_cash, cash=acct.cash, positions=pos, fills=fills, orders=orders
    )


def record_reconciliation(
    session: Session,
    audit: AuditLog,
    *,
    acct: PaperAccount,
    result: ReconciliationResult,
    at: datetime,
    trigger: str,
    actor_id: str,
) -> ReconciliationRow:
    row = ReconciliationRow(
        account_id=acct.id,
        run_at=at,
        scope="PAPER_FILL_JOURNAL",
        trigger=trigger,
        status="OK" if result.ok else "BREAK",
        diffs=[d.as_dict() for d in result.diffs],
        summary=result.summary,
    )
    session.add(row)
    session.flush()
    if not result.ok:
        audit.append(
            session,
            event_type="portfolio.reconciliation_break",
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            subject_type="paper_account",
            subject_id=str(acct.id),
            payload={"diffs": row.diffs[:20], "trigger": trigger},
        )
    return row


def latest_reconciliation(session: Session, account_id: int) -> ReconciliationRow | None:
    return session.execute(
        select(ReconciliationRow)
        .where(ReconciliationRow.account_id == account_id)
        .order_by(ReconciliationRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_reconciliations(
    session: Session, account_id: int, limit: int = 50
) -> list[ReconciliationRow]:
    return list(
        session.execute(
            select(ReconciliationRow)
            .where(ReconciliationRow.account_id == account_id)
            .order_by(ReconciliationRow.id.desc())
            .limit(limit)
        ).scalars()
    )
