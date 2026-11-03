"""The only output shape an assessment may take (strict: unknown fields are rejected).

There is deliberately no quantity, price, weight, limit or order field: an assessment cannot
express a trade, only a verdict on a candidate the rules already produced.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Action = Literal["CONFIRM", "HOLD", "NO_ACTION", "REQUIRES_REVIEW"]
ALLOWED_ACTIONS: tuple[str, ...] = ("CONFIRM", "HOLD", "NO_ACTION", "REQUIRES_REVIEW")

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
SourceIds = Annotated[
    list[Annotated[str, StringConstraints(max_length=80)]], Field(min_length=1, max_length=10)
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ObservedFact(_Strict):
    statement: Short
    source_ids: SourceIds


class KeyMetric(_Strict):
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=40)]
    source_ids: SourceIds


class Assessment(_Strict):
    symbol: Annotated[str, StringConstraints(max_length=32)]
    horizon: Literal["INTRADAY", "SWING", "LONG_TERM"]
    as_of: Annotated[str, StringConstraints(max_length=40)]
    freshness_status: Annotated[str, StringConstraints(max_length=20)]
    observed_facts: Annotated[list[ObservedFact], Field(max_length=20)]
    bull_case: Annotated[list[Short], Field(max_length=6)]
    bear_case: Annotated[list[Short], Field(max_length=6)]
    catalysts: Annotated[list[Short], Field(max_length=6)]
    invalidation_conditions: Annotated[list[Short], Field(max_length=6)]
    key_metrics_with_provenance: Annotated[list[KeyMetric], Field(max_length=20)]
    suggested_action: Action
    rationale: Annotated[str, StringConstraints(min_length=1, max_length=1500)]
    confidence_explanation: Annotated[str, StringConstraints(min_length=1, max_length=800)]
    key_uncertainties: Annotated[list[Short], Field(max_length=8)]
    change_view_conditions: Annotated[list[Short], Field(max_length=6)]
    evidence_sufficient: bool


def output_schema() -> dict[str, Any]:
    return Assessment.model_json_schema()
