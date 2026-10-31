"""Versioned transaction-cost model for Indian cash equity (intraday and delivery).

THE DEFAULT RATES ARE ILLUSTRATIVE AND UNVERIFIED. Statutory charges (STT, exchange
transaction charges, SEBI fees, stamp duty, GST) change over time and brokerage depends on
the owner's Sharekhan plan. Before results are relied on, replace them with values from a
current contract note / official circulars and bump ``version`` (BACKTESTING_METHODOLOGY.md).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

PAISA = Decimal("0.01")
CRORE = Decimal(10_000_000)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Product(StrEnum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"


@dataclass(frozen=True)
class Charges:
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    stamp: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp + self.gst

    def as_dict(self) -> dict[str, str]:
        d = {k: str(v) for k, v in asdict(self).items()}
        d["total"] = str(self.total)
        return d


ZERO_CHARGES = Charges(*(Decimal(0),) * 6)


@dataclass(frozen=True)
class CostModel:
    version: str = "illustrative-unverified-2026-10"
    verified: bool = False
    brokerage_rate_intraday: Decimal = Decimal("0.001")  # placeholder; plan-specific
    brokerage_rate_delivery: Decimal = Decimal("0.005")  # placeholder; plan-specific
    brokerage_min_per_order: Decimal = Decimal(0)
    stt_intraday_sell: Decimal = Decimal("0.00025")
    stt_delivery_both: Decimal = Decimal("0.001")
    exchange_txn_rate: Decimal = Decimal("0.0000297")
    sebi_per_crore: Decimal = Decimal(10)
    stamp_intraday_buy: Decimal = Decimal("0.00003")
    stamp_delivery_buy: Decimal = Decimal("0.00015")
    gst_rate: Decimal = Decimal("0.18")

    def charges(self, side: Side, quantity: int, price: Decimal, product: Product) -> Charges:
        if quantity <= 0:
            return ZERO_CHARGES
        value = price * quantity
        intraday = product is Product.INTRADAY
        rate = self.brokerage_rate_intraday if intraday else self.brokerage_rate_delivery
        brokerage = max(value * rate, self.brokerage_min_per_order)
        if intraday:
            stt = value * self.stt_intraday_sell if side is Side.SELL else Decimal(0)
            stamp = value * self.stamp_intraday_buy if side is Side.BUY else Decimal(0)
        else:
            stt = value * self.stt_delivery_both
            stamp = value * self.stamp_delivery_buy if side is Side.BUY else Decimal(0)
        exch = value * self.exchange_txn_rate
        sebi = value * self.sebi_per_crore / CRORE
        gst = (brokerage + exch + sebi) * self.gst_rate

        def q(x: Decimal) -> Decimal:
            return x.quantize(PAISA, rounding=ROUND_HALF_UP)

        return Charges(q(brokerage), q(stt), q(exch), q(sebi), q(stamp), q(gst))

    def describe(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
