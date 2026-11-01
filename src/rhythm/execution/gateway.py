"""Order venues for the PAPER book, and the UNKNOWN-state model.

The paper venue acknowledges immediately. ``FlakyVenue`` (tests) reproduces the failure that
matters most for a broker with no client order id (SHAREKHAN_INTEGRATION.md §4): a submit that
times out after the venue may already have accepted the order. Such an order goes to UNKNOWN;
it is never re-sent automatically. ``PaperBook.resolve_unknown`` matches it against the
venue's day order book, and an unresolved UNKNOWN blocks new orders on that instrument (R16).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from rhythm.core.errors import RhythmError
from rhythm.core.market import InstrumentKey
from rhythm.execution.costs import Side

if TYPE_CHECKING:
    from rhythm.execution.book import Order


class SubmissionTimeoutError(RhythmError):
    """No answer from the venue: the order may or may not exist there."""


class VenueRejectedError(RhythmError):
    """The venue definitively refused the order."""


@dataclass(frozen=True)
class VenueOrder:
    venue_order_id: str
    key: InstrumentKey
    side: Side
    quantity: int
    at: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None


class OrderVenue(Protocol):
    name: str

    def submit(self, order: Order, at: datetime) -> str: ...

    def day_orders(self) -> list[VenueOrder]: ...


@dataclass
class PaperVenue:
    """Simulated venue: every order is accepted and recorded."""

    name: str = "PAPER"
    orders: list[VenueOrder] = field(default_factory=list)
    _seq: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    def _record(self, order: Order, at: datetime) -> str:
        vid = f"{self.name}-{next(self._seq):08d}"
        self.orders.append(
            VenueOrder(
                vid, order.key, order.side, order.quantity, at, order.limit_price, order.stop_price
            )
        )
        return vid

    def submit(self, order: Order, at: datetime) -> str:
        return self._record(order, at)

    def day_orders(self) -> list[VenueOrder]:
        return list(self.orders)


@dataclass
class FlakyVenue(PaperVenue):
    """Failure injection for tests.

    ``mode``: ``timeout_after_accept`` (venue has the order, caller never heard back),
    ``timeout_before_accept`` (venue never got it), ``reject``. ``fail_next`` counts how many
    submissions fail; later ones behave normally.
    """

    name: str = "FLAKY"
    mode: str = "timeout_after_accept"
    fail_next: int = 1

    def submit(self, order: Order, at: datetime) -> str:
        if self.fail_next <= 0:
            return self._record(order, at)
        self.fail_next -= 1
        if self.mode == "timeout_after_accept":
            self._record(order, at)
            raise SubmissionTimeoutError("venue did not answer in time")
        if self.mode == "timeout_before_accept":
            raise SubmissionTimeoutError("venue did not answer in time")
        if self.mode == "reject":
            raise VenueRejectedError("rejected by venue (simulated)")
        raise ValueError(f"unknown failure mode {self.mode}")
