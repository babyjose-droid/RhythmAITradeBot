"""Deterministic MOCK quarterly fundamentals. Synthetic numbers, labelled MOCK.

Each instrument gets a stable "profile" (quality, valuation, growth) from a hash, so some
mock names pass the long-term screen and others do not. EPS is scaled to the instrument's
mock price so valuation ratios land in a plausible range. Results are "published" 45 days
after the quarter ends.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import DataOrigin, InstrumentKey
from rhythm.fundamentals.types import FundamentalSnapshot

PUBLISH_LAG_DAYS = 45
# Fixed anchor: the series must not depend on the query window, or a later sync would
# rewrite history (and backtests would disagree with PAPER cycles).
ANCHOR = date(2026, 12, 31)
SHARES = Decimal(100_000_000)


def _h(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def _quarter_ends(start: date, end: date) -> list[date]:
    out: list[date] = []
    for y in range(start.year - 3, end.year + 1):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            qe = date(y, m, d)
            if qe <= end:
                out.append(qe)
    return out


class MockFundamentalsProvider:
    name = "mock"
    origin = DataOrigin.MOCK

    def __init__(self, price_at: Callable[[InstrumentKey, datetime], Decimal], seed: int = 23):
        self.price_at = price_at
        self.seed = seed

    def _profile(self, key: InstrumentKey) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """(target P/E, ROE, debt/equity, quarterly EPS growth) — stable per instrument."""
        h = _h(self.seed, key)
        pe = Decimal(12 + h % 40)  # 12..51
        roe = Decimal(6 + (h >> 8) % 24) / 100  # 6..29 %
        de = Decimal((h >> 16) % 160) / 100  # 0..1.59
        growth = Decimal(((h >> 24) % 9) - 2) / 100  # -2..+6 % per quarter
        return pe, roe, de, growth

    def snapshots(
        self, key: InstrumentKey, start: date, end: date
    ) -> Sequence[FundamentalSnapshot]:
        pe, roe, de, growth = self._profile(key)
        ref_price = self.price_at(key, datetime.combine(ANCHOR, time(15, 30), tzinfo=EXCHANGE_TZ))
        eps_ttm_anchor = ref_price / pe
        out: list[FundamentalSnapshot] = []
        for qe in _quarter_ends(start, end):
            if not start <= qe <= end:
                continue
            back = ((ANCHOR.year - qe.year) * 12 + ANCHOR.month - qe.month) // 3
            eps_q = (eps_ttm_anchor / 4 / (1 + growth) ** back).quantize(Decimal("0.01"))
            ni = eps_q * SHARES
            equity = (ni * 4 / roe).quantize(Decimal(1))
            noise = Decimal(_h(self.seed, key, qe) % 21 - 10) / 100  # +/-10 % on OCF
            ocf = (ni * (Decimal("1.1") + noise)).quantize(Decimal(1))
            known = datetime.combine(
                qe + timedelta(days=PUBLISH_LAG_DAYS), time(18, 0), tzinfo=EXCHANGE_TZ
            )
            out.append(
                FundamentalSnapshot(
                    key=key,
                    period_end=qe,
                    known_at=known,
                    source=DataOrigin.MOCK.value,
                    source_ref=f"mock:{key}:{qe.isoformat()}",
                    metrics={
                        "revenue": (ni * 8).quantize(Decimal(1)),
                        "net_income": ni,
                        "eps": eps_q,
                        "book_value_per_share": (equity / SHARES).quantize(Decimal("0.01")),
                        "operating_cash_flow": ocf,
                        "total_debt": (equity * de).quantize(Decimal(1)),
                        "total_equity": equity,
                    },
                )
            )
        return out
