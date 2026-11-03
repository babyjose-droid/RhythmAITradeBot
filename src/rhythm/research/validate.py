"""Untrusted-output validation. Anything that fails yields no assessment (safe fallback)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import ValidationError

from rhythm.research.packet import EvidencePacket
from rhythm.research.schema import Assessment

MAX_RAW_CHARS = 20_000
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)
_TOKEN = re.compile(r"(?<![\w.])-?\d+(?:,\d{2,3})*(?:\.\d+)?(%?)")


@dataclass
class ValidationResult:
    ok: bool
    assessment: Assessment | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors}


def parse_json(raw: str) -> dict[str, Any]:
    if len(raw) > MAX_RAW_CHARS:
        raise ValueError(f"response too large ({len(raw)} chars > {MAX_RAW_CHARS})")
    text = raw.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    obj = json.loads(text)  # raises ValueError on malformed JSON or trailing text
    if not isinstance(obj, dict):
        raise ValueError("response must be a single JSON object")
    return obj


def _matches(value: Decimal, candidates: set[Decimal] | frozenset[Decimal] | list[Decimal]) -> bool:
    """True if ``value`` equals some candidate rounded to ``value``'s own precision."""
    exp = value.as_tuple().exponent
    q = Decimal(1).scaleb(exp) if isinstance(exp, int) else Decimal(1)
    for c in candidates:
        for form in (c, c * 100):  # 0.266 may be written as 26.6 (%)
            if form.quantize(q, rounding=ROUND_HALF_UP) == value:
                return True
    return False


def _text_numbers(text: str) -> list[Decimal]:
    out = []
    for m in _TOKEN.finditer(text):
        tok = m.group(0).rstrip("%").replace(",", "")
        out.append(Decimal(tok))
    return out


def validate(raw: str, packet: EvidencePacket) -> ValidationResult:
    try:
        obj = parse_json(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        return ValidationResult(False, errors=[f"malformed output: {str(exc)[:200]}"])
    try:
        a = Assessment.model_validate(obj)
    except ValidationError as exc:
        errs = [
            f"schema: {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors()[:10]
        ]
        return ValidationResult(False, errors=errs)

    errors: list[str] = []
    if a.symbol != packet.symbol or a.horizon != packet.horizon:
        errors.append("symbol/horizon do not match the packet")
    if a.as_of != packet.as_of:
        errors.append("as_of does not match the packet")
    known = packet.source_ids
    cited = [i for f in a.observed_facts for i in f.source_ids] + [
        i for k in a.key_metrics_with_provenance for i in k.source_ids
    ]
    unknown = sorted({i for i in cited if i not in known})
    if unknown:
        errors.append(f"unknown source ids: {unknown[:5]}")
    for k in a.key_metrics_with_provenance:
        try:
            v = Decimal(k.value.rstrip("%").replace(",", ""))
        except ArithmeticError:
            continue  # non-numeric metric value (e.g. "positive"): allowed, it is cited
        vals = packet.values_of(k.source_ids)
        if not vals or not _matches(v, vals):
            errors.append(f"key metric {k.name}={k.value} does not match its cited sources")
    nums = packet.numbers
    texts = (
        [f.statement for f in a.observed_facts]
        + list(a.bull_case)
        + list(a.bear_case)
        + list(a.catalysts)
        + list(a.invalidation_conditions)
        + list(a.key_uncertainties)
        + list(a.change_view_conditions)
        + [a.rationale, a.confidence_explanation]
    )
    stray = sorted({str(n) for t in texts for n in _text_numbers(t) if not _matches(n, nums)})
    if stray:
        errors.append(f"numbers not present in the packet: {stray[:5]}")
    if a.suggested_action == "CONFIRM" and not a.evidence_sufficient:
        errors.append("CONFIRM requires evidence_sufficient = true")
    if errors:
        return ValidationResult(False, errors=errors)
    return ValidationResult(True, assessment=a)
