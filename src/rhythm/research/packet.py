"""Frozen evidence packets: the only information an assessment may use.

A packet lists facts (each with an id, value, unit, source and source time), derived metrics
(with formula and input ids), assumptions, known gaps (``missing``) and untrusted documents
(quoted text that is data, never instructions). The packet is canonical JSON with a SHA-256
digest, stored before any model call so every assessment can be traced to its inputs.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from rhythm.core.clock import ensure_utc
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.events.types import CorporateEvent
from rhythm.fundamentals.types import FundamentalSnapshot, derive, known_as_of

PACKET_SCHEMA_VERSION = "packet-1.0"
MAX_DOCUMENTS = 5
MAX_DOCUMENT_CHARS = 4000
MAX_FACTS = 120
# Text that could close or forge the packet's delimiters is removed from untrusted documents.
_DELIMITER = re.compile(
    r"</?\s*(evidence_packet|untrusted_document|system|instructions)\b[^>]*>?", re.I
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
# Diagnostics never copied into a packet (bulky or already represented elsewhere).
_SKIP_DIAG = frozenset({"metrics", "checks_detail", "bars"})


@dataclass(frozen=True)
class Fact:
    id: str
    name: str
    value: str
    unit: str = ""
    source: str = ""
    source_ts: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_ts": self.source_ts,
        }


@dataclass(frozen=True)
class DerivedMetric:
    id: str
    name: str
    value: str | None
    formula: str
    inputs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "value": self.value,
            "formula": self.formula,
            "inputs": list(self.inputs),
        }


@dataclass(frozen=True)
class UntrustedDocument:
    id: str
    source: str
    retrieved_at: str
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "retrieved_at": self.retrieved_at,
            "trust": "untrusted",
            "text": self.text,
        }


def sanitize_document(text: str) -> str:
    """Normalise, strip control characters and delimiter look-alikes, cap the length."""
    t = unicodedata.normalize("NFKC", text)
    t = _CONTROL.sub(" ", t)
    t = _DELIMITER.sub("[removed tag]", t)
    if len(t) > MAX_DOCUMENT_CHARS:
        t = t[:MAX_DOCUMENT_CHARS] + " [truncated]"
    return t


@dataclass(frozen=True)
class CandidateView:
    """A rule-generated candidate, as the research service sees it (never an order)."""

    key: InstrumentKey
    horizon: Horizon
    signal_type: str
    at: datetime
    strategy: str
    version: str
    ref_price: Decimal
    data_is_mock: bool
    stop: Decimal | None = None
    target: Decimal | None = None
    target_weight: Decimal | None = None
    reasons: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    risk: Mapping[str, Any] = field(default_factory=dict)
    quote_status: str | None = None


@dataclass(frozen=True)
class EvidencePacket:
    symbol: str
    exchange: str
    horizon: str
    as_of: str
    candidate: dict[str, Any]
    freshness: dict[str, Any]
    facts: tuple[Fact, ...]
    derived: tuple[DerivedMetric, ...]
    assumptions: tuple[str, ...]
    missing: tuple[str, ...]
    untrusted_documents: tuple[UntrustedDocument, ...]
    data_is_mock: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PACKET_SCHEMA_VERSION,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "horizon": self.horizon,
            "as_of": self.as_of,
            "data_is_mock": self.data_is_mock,
            "candidate": self.candidate,
            "freshness": self.freshness,
            "facts": [f.as_dict() for f in self.facts],
            "derived_metrics": [d.as_dict() for d in self.derived],
            "assumptions": list(self.assumptions),
            "missing": list(self.missing),
            "untrusted_documents": [d.as_dict() for d in self.untrusted_documents],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    @property
    def source_ids(self) -> frozenset[str]:
        ids = {f.id for f in self.facts} | {d.id for d in self.derived}
        return frozenset(ids | {d.id for d in self.untrusted_documents} | {"candidate"})

    def values_of(self, ids: Sequence[str]) -> list[Decimal]:
        """Numeric values carried by the given fact / derived ids."""
        out: list[Decimal] = []
        by_id = {f.id: f.value for f in self.facts} | {d.id: d.value for d in self.derived}
        for i in ids:
            n = _num(by_id.get(i))
            if n is not None:
                out.append(n)
        return out

    @property
    def numbers(self) -> frozenset[Decimal]:
        """Every number that appears in the packet (facts, derived, candidate, dates)."""
        nums: set[Decimal] = set()
        for v in _walk(self.as_dict() | {"untrusted_documents": []}):
            if isinstance(v, bool):
                continue
            if isinstance(v, int | float | Decimal):
                nums.add(Decimal(str(v)))
            elif isinstance(v, str):
                for tok in re.findall(r"-?\d+(?:\.\d+)?", v):
                    nums.add(Decimal(tok))
        return frozenset(nums)


def _walk(o: Any) -> list[Any]:
    if isinstance(o, dict):
        return [x for v in o.values() for x in _walk(v)]
    if isinstance(o, list | tuple):
        return [x for v in o for x in _walk(v)]
    return [o]


def _num(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        s = str(v).strip()
        return Decimal(s) if _NUMBER.match(s) else None
    except InvalidOperation:
        return None


def _scalar(v: Any) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | Decimal):
        return str(v)
    if isinstance(v, float):
        return str(round(v, 6))
    if isinstance(v, str) and len(v) <= 200:
        return v
    return None


def build_packet(
    c: CandidateView,
    *,
    fundamentals: Sequence[FundamentalSnapshot] = (),
    events: Sequence[CorporateEvent] = (),
    documents: Sequence[tuple[str, str]] = (),
    event_calendar_synced: bool | None = None,
) -> EvidencePacket:
    """Assemble the packet from point-in-time inputs known at ``c.at``.

    ``documents`` are (source, text) pairs supplied by a person or a feed. They are
    sanitised and always marked untrusted.
    """
    at = ensure_utc(c.at)
    as_of = at.isoformat()
    origin = "MOCK" if c.data_is_mock else "market_data"
    facts: list[Fact] = [
        Fact(
            "price.ref",
            "reference price (last close / bar close)",
            str(c.ref_price),
            "INR",
            origin,
            as_of,
        )
    ]
    if c.stop is not None:
        facts.append(
            Fact("candidate.stop", "strategy stop price", str(c.stop), "INR", "strategy", as_of)
        )
    if c.target is not None:
        facts.append(
            Fact(
                "candidate.target", "strategy target price", str(c.target), "INR", "strategy", as_of
            )
        )
    if c.target_weight is not None:
        facts.append(
            Fact(
                "candidate.target_weight",
                "target weight of ledger",
                str(c.target_weight),
                "fraction",
                "strategy",
                as_of,
            )
        )
    for k, v in sorted(c.diagnostics.items()):
        if k in _SKIP_DIAG:
            continue
        if isinstance(v, Mapping):
            for sk, sv in sorted(v.items()):
                s = _scalar(sv)
                if s is not None:
                    facts.append(
                        Fact(
                            f"diag.{k}.{sk}",
                            f"strategy diagnostic {k}.{sk}",
                            s,
                            "",
                            "strategy",
                            as_of,
                        )
                    )
        else:
            s = _scalar(v)
            if s is not None:
                facts.append(
                    Fact(f"diag.{k}", f"strategy diagnostic {k}", s, "", "strategy", as_of)
                )
    risk_outcome = c.risk.get("outcome")
    if risk_outcome is not None:
        facts.append(
            Fact(
                "risk.outcome",
                "independent risk engine outcome",
                str(risk_outcome),
                "",
                "risk_engine",
                as_of,
            )
        )
        for chk in c.risk.get("checks", []):
            if isinstance(chk, Mapping) and chk.get("status") == "FAIL":
                facts.append(
                    Fact(
                        f"risk.fail.{chk.get('code')}",
                        "failed risk check",
                        str(chk.get("reason", ""))[:200],
                        "",
                        "risk_engine",
                        as_of,
                    )
                )

    derived: list[DerivedMetric] = []
    missing: list[str] = []
    quarters = known_as_of(fundamentals, at)
    if quarters:
        for q in quarters[-4:]:
            pid = q.period_end.isoformat()
            for m, val in sorted(q.metrics.items()):
                facts.append(
                    Fact(
                        f"fund.{pid}.{m}",
                        f"{m} for quarter ending {pid}",
                        str(val),
                        "INR" if m not in ("eps", "book_value_per_share") else "INR/share",
                        q.source,
                        q.known_at.isoformat(),
                    )
                )
        refs = tuple(f"fund.{q.period_end.isoformat()}.eps" for q in quarters[-4:])
        for name, d in sorted(derive(quarters, c.ref_price).items()):
            derived.append(
                DerivedMetric(
                    f"derived.{name}",
                    name,
                    None if d.value is None else str(round(d.value, 4)),
                    d.formula,
                    ("price.ref", *refs),
                )
            )
    elif c.horizon is Horizon.LONG_TERM:
        missing.append("no published fundamentals")

    upcoming = sorted(
        (e for e in events if e.known_at <= at and e.event_date >= at.date()),
        key=lambda e: e.event_date,
    )
    docs: list[UntrustedDocument] = []
    for i, e in enumerate(upcoming[:5]):
        facts.append(
            Fact(
                f"event.{i}",
                f"known upcoming {e.kind.value}",
                e.event_date.isoformat(),
                "date",
                str(e.source),
                e.known_at.isoformat(),
            )
        )
        if e.note:
            docs.append(
                UntrustedDocument(
                    f"doc.event.{i}",
                    f"event note ({e.source})",
                    e.known_at.isoformat(),
                    sanitize_document(e.note),
                )
            )
    if event_calendar_synced is False:
        missing.append("event calendar not synced")
    for i, (src, text) in enumerate(documents[: MAX_DOCUMENTS - len(docs)]):
        docs.append(
            UntrustedDocument(
                f"doc.{i}", sanitize_document(src)[:120], as_of, sanitize_document(text)
            )
        )
    if not documents:
        missing.append("no news or filings supplied (no licensed source configured)")
    missing.append("sector and correlation data unavailable")

    assumptions = [
        "the transaction-cost model is illustrative and unverified",
        "an assessment may only confirm the candidate or downgrade it "
        "(HOLD, NO_ACTION or REQUIRES_REVIEW)",
    ]
    if c.data_is_mock:
        assumptions.insert(
            0, "all market and fundamental data in this packet is synthetic MOCK data"
        )

    return EvidencePacket(
        symbol=c.key.symbol,
        exchange=c.key.exchange.value,
        horizon=c.horizon.value,
        as_of=as_of,
        candidate={
            "type": c.signal_type,
            "strategy": f"{c.strategy}@{c.version}",
            "reasons": list(c.reasons),
            "risk_outcome": risk_outcome,
        },
        freshness={"quote_status": c.quote_status or "UNKNOWN", "as_of": as_of},
        facts=tuple(facts[:MAX_FACTS]),
        derived=tuple(derived),
        assumptions=tuple(assumptions),
        missing=tuple(missing),
        untrusted_documents=tuple(docs),
        data_is_mock=c.data_is_mock,
    )
