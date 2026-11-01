"""In-memory PAPER book: cash, long-only positions, orders, fills and the event trail.

Used identically by the backtester and by the persisted paper-trading service, so both
exercise the same order lifecycle, fill and cost logic. It never talks to a broker.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from rhythm.core.errors import RhythmError
from rhythm.core.market import Horizon, InstrumentKey
from rhythm.execution.costs import Charges, CostModel, Product, Side
from rhythm.execution.fills import FillModel, OrderType
from rhythm.execution.gateway import (
    OrderVenue,
    PaperVenue,
    SubmissionTimeoutError,
    VenueRejectedError,
)
from rhythm.marketdata.types import Bar
from rhythm.orders.state import TERMINAL, WORKING, OrderState, check_transition

ACCOUNT_KIND = "PAPER"


class BookError(RhythmError):
    pass


@dataclass
class Order:
    id: str
    key: InstrumentKey
    side: Side
    order_type: OrderType
    quantity: int
    created_at: datetime
    idempotency_key: str
    product: Product = Product.INTRADAY
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    state: OrderState = OrderState.PROPOSED
    filled_quantity: int = 0
    average_fill_price: Decimal | None = None
    strategy: str | None = None
    purpose: str = "entry"  # entry | exit | stop | target | square_off
    oco_group: str | None = None
    reason: str | None = None
    updated_at: datetime | None = None
    venue_order_id: str | None = None

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity


@dataclass(frozen=True)
class Fill:
    id: str
    order_id: str
    key: InstrumentKey
    side: Side
    quantity: int
    price: Decimal
    at: datetime
    charges: Charges


@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    from_state: OrderState | None
    to_state: OrderState
    at: datetime
    detail: dict[str, Any]


@dataclass
class Position:
    key: InstrumentKey
    quantity: int = 0
    average_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    charges_paid: Decimal = Decimal(0)
    opened_at: datetime | None = None
    stop: Decimal | None = None
    target: Decimal | None = None


@dataclass
class PaperBook:
    cash: Decimal
    cost_model: CostModel = field(default_factory=CostModel)
    fill_model: FillModel = field(default_factory=FillModel)
    ticks: dict[InstrumentKey, Decimal] = field(default_factory=dict)
    positions: dict[InstrumentKey, Position] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)
    events: list[OrderEvent] = field(default_factory=list)
    id_factory: Callable[[str], str] | None = None
    venue: OrderVenue = field(default_factory=PaperVenue)
    horizon: Horizon = Horizon.INTRADAY
    flow: Decimal = Decimal(0)  # net cash flow from this book's fills (ledger-scoped P&L)
    _seq: Iterator[int] = field(default_factory=lambda: itertools.count(1))
    _by_idem: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ ids / helpers
    def _new_id(self, prefix: str) -> str:
        if self.id_factory is not None:
            return self.id_factory(prefix)
        return f"{prefix}-{next(self._seq):06d}"

    def tick(self, key: InstrumentKey) -> Decimal:
        return self.ticks.get(key, Decimal("0.05"))

    def position(self, key: InstrumentKey) -> Position:
        return self.positions.setdefault(key, Position(key))

    def working_orders(self, key: InstrumentKey | None = None) -> list[Order]:
        return [
            o for o in self.orders.values() if o.state in WORKING and (key is None or o.key == key)
        ]

    def open_order_count(self) -> int:
        return sum(1 for o in self.orders.values() if o.state not in TERMINAL)

    def transition(self, order: Order, to: OrderState, at: datetime, **detail: Any) -> None:
        check_transition(order.state, to)
        self.events.append(OrderEvent(order.id, order.state, to, at, detail))
        order.state = to
        order.updated_at = at

    # ------------------------------------------------------------------ lifecycle
    def propose(
        self,
        *,
        key: InstrumentKey,
        side: Side,
        order_type: OrderType,
        quantity: int,
        at: datetime,
        idempotency_key: str,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        strategy: str | None = None,
        purpose: str = "entry",
        oco_group: str | None = None,
        product: Product = Product.INTRADAY,
    ) -> tuple[Order, bool]:
        """Create a PROPOSED order. Returns (order, created). Duplicate keys return the original."""
        if idempotency_key in self._by_idem:
            return self.orders[self._by_idem[idempotency_key]], False
        if quantity <= 0:
            raise BookError("quantity must be positive")
        order = Order(
            id=self._new_id("PO"),
            key=key,
            side=side,
            order_type=order_type,
            quantity=quantity,
            created_at=at,
            idempotency_key=idempotency_key,
            limit_price=limit_price,
            stop_price=stop_price,
            strategy=strategy,
            purpose=purpose,
            oco_group=oco_group,
            product=product,
            updated_at=at,
        )
        self.orders[order.id] = order
        self._by_idem[idempotency_key] = order.id
        self.events.append(
            OrderEvent(order.id, None, OrderState.PROPOSED, at, {"purpose": purpose})
        )
        return order, True

    def submit(self, order: Order, at: datetime) -> None:
        """RISK_CHECKED -> SUBMITTED -> BROKER_ACKNOWLEDGED | UNKNOWN | BROKER_REJECTED.

        A timeout leaves the order UNKNOWN. It is never re-sent automatically; see
        ``resolve_unknown``.
        """
        name = self.venue.name
        self.transition(order, OrderState.SUBMITTED, at, venue=name)
        try:
            vid = self.venue.submit(order, at)
        except SubmissionTimeoutError as exc:
            order.reason = "submission timed out; venue state unknown"
            self.transition(order, OrderState.UNKNOWN, at, venue=name, error=str(exc))
            return
        except VenueRejectedError as exc:
            order.reason = str(exc)[:200]
            self.transition(order, OrderState.BROKER_REJECTED, at, venue=name, error=str(exc))
            return
        order.venue_order_id = vid
        self.transition(order, OrderState.BROKER_ACKNOWLEDGED, at, venue=name, venue_order_id=vid)

    def resolve_unknown(
        self, at: datetime, *, match_window_seconds: int = 120, give_up_after_seconds: int = 300
    ) -> dict[str, str]:
        """Match UNKNOWN orders against the venue's day order book. Returns id -> outcome.

        Exactly one unclaimed venue order with the same instrument, side and quantity, placed
        within the window -> BROKER_ACKNOWLEDGED. None found after ``give_up_after_seconds``
        -> CANCELLED (treated as never placed). Several candidates -> stays UNKNOWN (ambiguous,
        needs a human).
        """
        unknown = [o for o in self.orders.values() if o.state is OrderState.UNKNOWN]
        if not unknown:
            return {}
        claimed = {o.venue_order_id for o in self.orders.values() if o.venue_order_id}
        venue_orders = self.venue.day_orders()
        out: dict[str, str] = {}
        for o in sorted(unknown, key=lambda x: (x.created_at, x.id)):
            matches = [
                v
                for v in venue_orders
                if v.venue_order_id not in claimed
                and v.key == o.key
                and v.side is o.side
                and v.quantity == o.quantity
                and abs((v.at - o.created_at).total_seconds()) <= match_window_seconds
            ]
            if len(matches) == 1:
                vid = matches[0].venue_order_id
                o.venue_order_id = vid
                claimed.add(vid)
                o.reason = None
                self.transition(
                    o, OrderState.BROKER_ACKNOWLEDGED, at, venue_order_id=vid, resolved="matched"
                )
                out[o.id] = "ACKNOWLEDGED"
            elif len(matches) > 1:
                out[o.id] = "AMBIGUOUS"
            elif (at - o.created_at).total_seconds() >= give_up_after_seconds:
                o.reason = "not found at venue; treated as never placed"
                self.transition(o, OrderState.CANCELLED, at, resolved="not found at venue")
                out[o.id] = "NOT_FOUND"
            else:
                out[o.id] = "PENDING"
        return out

    def cancel(self, order: Order, at: datetime, reason: str) -> None:
        if order.state in TERMINAL:
            return
        order.reason = reason
        self.transition(order, OrderState.CANCELLED, at, reason=reason)

    def expire_working(self, at: datetime, key: InstrumentKey | None = None) -> int:
        n = 0
        for o in self.working_orders(key):
            o.reason = "session end"
            self.transition(o, OrderState.EXPIRED, at, reason="session end")
            n += 1
        return n

    # ------------------------------------------------------------------ fills
    def apply_fill(self, order: Order, quantity: int, price: Decimal, at: datetime) -> Fill:
        pos = self.position(order.key)
        if order.side is Side.SELL and quantity > pos.quantity:
            raise BookError("sell exceeds position (short selling is not supported)")
        charges = self.cost_model.charges(order.side, quantity, price, order.product)
        value = price * quantity
        if order.side is Side.BUY:
            new_qty = pos.quantity + quantity
            pos.average_price = (pos.average_price * pos.quantity + value) / new_qty
            pos.quantity = new_qty
            pos.opened_at = pos.opened_at or at
            self.cash -= value + charges.total
            self.flow -= value + charges.total
        else:
            pos.realized_pnl += (price - pos.average_price) * quantity
            pos.quantity -= quantity
            self.cash += value - charges.total
            self.flow += value - charges.total
            if pos.quantity == 0:
                pos.average_price = Decimal(0)
                pos.opened_at = None
                pos.stop = pos.target = None
        pos.charges_paid += charges.total
        prev_value = (order.average_fill_price or Decimal(0)) * order.filled_quantity
        order.filled_quantity += quantity
        order.average_fill_price = (prev_value + value) / order.filled_quantity
        fill = Fill(
            self._new_id("PF"), order.id, order.key, order.side, quantity, price, at, charges
        )
        self.fills.append(fill)
        to = OrderState.FILLED if order.remaining == 0 else OrderState.PARTIALLY_FILLED
        self.transition(order, to, at, fill_id=fill.id, quantity=quantity, price=str(price))
        return fill

    def on_bar(self, bar: Bar, *, purposes: frozenset[str] | None = None) -> list[Fill]:
        """Fill working orders for ``bar.key`` against this bar (protective stop before target)."""
        made: list[Fill] = []
        capacity = self.fill_model.available(bar)
        priority = {"stop": 0, "square_off": 1, "exit": 1, "entry": 2, "target": 3}
        candidates = [
            o for o in self.working_orders(bar.key) if purposes is None or o.purpose in purposes
        ]
        for order in sorted(candidates, key=lambda o: (priority[o.purpose], o.id)):
            if order.state not in WORKING:  # cancelled as an OCO sibling during this loop
                continue
            remaining = order.remaining
            if order.side is Side.SELL:
                remaining = min(remaining, self.position(order.key).quantity)
                if remaining == 0:
                    self.cancel(order, bar.start, "no position to sell")
                    continue
            quote = self.fill_model.simulate(
                side=order.side,
                order_type=order.order_type,
                remaining=remaining,
                bar=bar,
                tick=self.tick(order.key),
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                capacity=capacity,
            )
            if quote is None:
                continue
            made.append(self.apply_fill(order, quote.quantity, quote.price, bar.start))
            capacity -= quote.quantity
            if order.oco_group:
                for sib in self.working_orders(order.key):
                    if sib.oco_group == order.oco_group and sib.id != order.id:
                        self.cancel(sib, bar.start, "oco sibling filled")
        return made

    def force_close(
        self, key: InstrumentKey, price: Decimal, at: datetime, reason: str
    ) -> Fill | None:
        """Simulated broker square-off at ``price`` (used at session end as a safety net)."""
        pos = self.position(key)
        for o in self.working_orders(key):
            self.cancel(o, at, reason)
        if pos.quantity <= 0:
            return None
        order, _ = self.propose(
            key=key,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=pos.quantity,
            at=at,
            idempotency_key=f"square_off:{key}:{at.isoformat()}",
            purpose="square_off",
        )
        self.transition(
            order, OrderState.RISK_CHECKED, at, gate="venue auto square-off (simulated)"
        )
        self.transition(order, OrderState.SUBMITTED, at, venue="PAPER")
        self.transition(order, OrderState.BROKER_ACKNOWLEDGED, at, venue="PAPER")
        return self.apply_fill(order, pos.quantity, price, at)

    # ------------------------------------------------------------------ valuation
    def equity(self, marks: dict[InstrumentKey, Decimal]) -> Decimal:
        held = sum(
            (marks.get(k, p.average_price) * p.quantity for k, p in self.positions.items()),
            Decimal(0),
        )
        return self.cash + held
