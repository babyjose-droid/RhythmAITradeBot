"""Order lifecycle state machine (SYSTEM_ARCHITECTURE.md §6). Illegal transitions raise."""

from __future__ import annotations

from enum import StrEnum

from rhythm.core.errors import RhythmError


class OrderState(StrEnum):
    PROPOSED = "PROPOSED"
    RISK_CHECKED = "RISK_CHECKED"
    RISK_REJECTED = "RISK_REJECTED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    SUBMITTED = "SUBMITTED"
    BROKER_ACKNOWLEDGED = "BROKER_ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    BROKER_REJECTED = "BROKER_REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"


class DisplayCategory(StrEnum):
    """What the UI must show. These four must never be visually confused."""

    APPROVED_NOT_SENT = "APPROVED_NOT_SENT"
    AT_BROKER_NOT_FILLED = "AT_BROKER_NOT_FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CLOSED_WITHOUT_FILL = "CLOSED_WITHOUT_FILL"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"


S = OrderState
TERMINAL = frozenset(
    {S.FILLED, S.CANCELLED, S.BROKER_REJECTED, S.EXPIRED, S.RISK_REJECTED, S.APPROVAL_REJECTED}
)
WORKING = frozenset({S.BROKER_ACKNOWLEDGED, S.PARTIALLY_FILLED})

_ALLOWED: dict[OrderState, frozenset[OrderState]] = {
    S.PROPOSED: frozenset({S.RISK_CHECKED, S.RISK_REJECTED}),
    S.RISK_CHECKED: frozenset({S.APPROVAL_PENDING, S.SUBMITTED, S.CANCELLED}),
    S.APPROVAL_PENDING: frozenset({S.APPROVED, S.APPROVAL_REJECTED}),
    S.APPROVED: frozenset({S.SUBMITTED, S.CANCELLED}),
    S.SUBMITTED: frozenset({S.BROKER_ACKNOWLEDGED, S.BROKER_REJECTED, S.UNKNOWN}),
    S.UNKNOWN: frozenset(
        {S.BROKER_ACKNOWLEDGED, S.BROKER_REJECTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED}
    ),
    S.BROKER_ACKNOWLEDGED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED, S.BROKER_REJECTED}
    ),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED}),
    **{t: frozenset({S.RECONCILED}) for t in TERMINAL},
    S.RECONCILED: frozenset(),
}


class IllegalTransitionError(RhythmError):
    pass


def check_transition(current: OrderState, to: OrderState) -> None:
    if to not in _ALLOWED[current]:
        raise IllegalTransitionError(f"{current.value} -> {to.value} is not allowed")


def allowed_next(current: OrderState) -> frozenset[OrderState]:
    return _ALLOWED[current]


def display_category(state: OrderState, filled_qty: int) -> DisplayCategory:
    if state in (S.PROPOSED, S.RISK_CHECKED, S.APPROVAL_PENDING, S.APPROVED):
        return DisplayCategory.APPROVED_NOT_SENT
    if state in (S.SUBMITTED, S.BROKER_ACKNOWLEDGED):
        return DisplayCategory.AT_BROKER_NOT_FILLED
    if state is S.PARTIALLY_FILLED:
        return DisplayCategory.PARTIALLY_FILLED
    if state is S.FILLED:
        return DisplayCategory.FILLED
    if state is S.UNKNOWN:
        return DisplayCategory.NEEDS_ATTENTION
    if state is S.RECONCILED:
        return DisplayCategory.FILLED if filled_qty > 0 else DisplayCategory.CLOSED_WITHOUT_FILL
    # cancelled / expired / rejected — may still carry partial fills
    return (
        DisplayCategory.PARTIALLY_FILLED if filled_qty > 0 else DisplayCategory.CLOSED_WITHOUT_FILL
    )
