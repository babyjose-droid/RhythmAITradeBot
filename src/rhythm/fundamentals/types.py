"""Point-in-time company fundamentals with provenance.

A snapshot is one reported quarter. ``known_at`` is when it became public; strategies and
the risk engine only see snapshots with ``known_at <= decision time``. Every number keeps its
source and period so a long-term thesis can cite exactly what it relied on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from rhythm.core.clock import ensure_utc
from rhythm.core.market import DataOrigin, InstrumentKey

# Reported per quarter (absolute INR or per-share INR).
REPORTED = (
    "revenue",
    "net_income",
    "eps",
    "book_value_per_share",
    "operating_cash_flow",
    "total_debt",
    "total_equity",
)


@dataclass(frozen=True)
class FundamentalSnapshot:
    key: InstrumentKey
    period_end: date
    known_at: datetime
    source: str
    metrics: Mapping[str, Decimal] = field(default_factory=dict)
    source_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_at", ensure_utc(self.known_at))
        missing = [m for m in REPORTED if m not in self.metrics]
        if missing:
            raise ValueError(f"snapshot missing metrics: {missing}")

    @property
    def is_mock(self) -> bool:
        return self.source == DataOrigin.MOCK.value

    def provenance(self, metric: str) -> dict[str, Any]:
        return {
            "value": str(self.metrics[metric]),
            "source": self.source,
            "period_end": self.period_end.isoformat(),
            "known_at": self.known_at.isoformat(),
            "source_ref": self.source_ref,
        }


class FundamentalsProvider(Protocol):
    name: str
    origin: DataOrigin

    def snapshots(
        self, key: InstrumentKey, start: date, end: date
    ) -> Sequence[FundamentalSnapshot]: ...


def known_as_of(snaps: Sequence[FundamentalSnapshot], as_of: datetime) -> list[FundamentalSnapshot]:
    """Snapshots public at ``as_of``, oldest period first (latest restatement wins)."""
    as_of = ensure_utc(as_of)
    by_period: dict[date, FundamentalSnapshot] = {}
    for s in sorted(snaps, key=lambda s: (s.period_end, s.known_at)):
        if s.known_at <= as_of:
            by_period[s.period_end] = s
    return [by_period[p] for p in sorted(by_period)]


# ---------------------------------------------------------------------------- derived metrics
@dataclass(frozen=True)
class Derived:
    """A metric computed from reported numbers, with its formula and inputs (provenance)."""

    name: str
    value: Decimal | None
    formula: str
    inputs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": None if self.value is None else str(round(self.value, 4)),
            "formula": self.formula,
            "inputs": list(self.inputs),
        }


def derive(quarters: Sequence[FundamentalSnapshot], price: Decimal) -> dict[str, Derived]:
    """TTM metrics from the latest 4 known quarters (+4 earlier quarters for growth)."""
    q = list(quarters)
    out: dict[str, Derived] = {}
    if len(q) < 4:
        return out
    last4 = q[-4:]
    refs = tuple(f"{s.source}:{s.period_end.isoformat()}" for s in last4)
    eps_ttm = sum((s.metrics["eps"] for s in last4), Decimal(0))
    ni_ttm = sum((s.metrics["net_income"] for s in last4), Decimal(0))
    ocf_ttm = sum((s.metrics["operating_cash_flow"] for s in last4), Decimal(0))
    latest = last4[-1]
    equity = latest.metrics["total_equity"]
    out["eps_ttm"] = Derived("eps_ttm", eps_ttm, "sum(eps, last 4 quarters)", refs)
    out["ocf_ttm"] = Derived("ocf_ttm", ocf_ttm, "sum(operating_cash_flow, last 4q)", refs)
    out["roe"] = Derived(
        "roe",
        ni_ttm / equity if equity > 0 else None,
        "sum(net_income, last 4q) / total_equity (latest)",
        refs,
    )
    out["debt_to_equity"] = Derived(
        "debt_to_equity",
        latest.metrics["total_debt"] / equity if equity > 0 else None,
        "total_debt / total_equity (latest)",
        refs[-1:],
    )
    out["pe"] = Derived(
        "pe",
        price / eps_ttm if eps_ttm > 0 else None,
        "price / eps_ttm",
        (*refs, "price:last_close"),
    )
    bvps = latest.metrics["book_value_per_share"]
    out["pb"] = Derived(
        "pb", price / bvps if bvps > 0 else None, "price / book_value_per_share", (refs[-1],)
    )
    if len(q) >= 8:
        prev4 = q[-8:-4]
        prev = sum((s.metrics["eps"] for s in prev4), Decimal(0))
        out["eps_growth_yoy"] = Derived(
            "eps_growth_yoy",
            (eps_ttm - prev) / abs(prev) if prev != 0 else None,
            "(eps_ttm - eps_ttm_4q_earlier) / |eps_ttm_4q_earlier|",
            tuple(f"{s.source}:{s.period_end.isoformat()}" for s in q[-8:]),
        )
    return out
