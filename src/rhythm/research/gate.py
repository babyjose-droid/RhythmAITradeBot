"""What an assessment may do to a candidate: keep it, or downgrade it. Never more.

* Risk-reducing candidates (EXIT, REDUCE) are never blocked by research: exits stay with the
  rules and the risk engine.
* Entries/adds proceed only on a valid CONFIRM with sufficient evidence. Anything else —
  HOLD, NO_ACTION, REQUIRES_REVIEW, invalid output, timeouts, budget exhaustion — keeps the
  candidate out of the order path (fail closed).
* There is no input that increases size: the gate returns allow / not allow, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from rhythm.research.schema import Assessment

RISK_INCREASING = frozenset({"ENTER_LONG", "ADD_LONG"})


@dataclass(frozen=True)
class GateOutcome:
    allow: bool
    decision: str  # a DecisionState value
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"allow": self.allow, "decision": self.decision, "reason": self.reason}


def gate(signal_type: str, status: str, assessment: Assessment | None) -> GateOutcome:
    if signal_type not in RISK_INCREASING:
        return GateOutcome(True, "UNCHANGED", "risk-reducing candidates are not gated by research")
    if status != "VALID" or assessment is None:
        return GateOutcome(
            False, "REQUIRES_REVIEW", f"no valid assessment ({status}): held for review"
        )
    act = assessment.suggested_action
    if act == "CONFIRM" and assessment.evidence_sufficient:
        return GateOutcome(True, "UNCHANGED", "assessment confirms the rule-generated candidate")
    if act == "HOLD":
        return GateOutcome(False, "HOLD", "assessment downgraded the candidate to HOLD")
    if act == "NO_ACTION":
        return GateOutcome(False, "NO_ACTION", "assessment downgraded the candidate to NO_ACTION")
    return GateOutcome(False, "REQUIRES_REVIEW", "assessment asks for human review")
