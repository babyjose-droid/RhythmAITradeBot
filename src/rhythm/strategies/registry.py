"""All available strategies by name. Adding one here exposes it to backtests and cycles."""

from __future__ import annotations

from rhythm.strategies.base import Strategy
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from rhythm.strategies.long_term import LongTermQualityValue
from rhythm.strategies.swing_trend import SwingTrendPullback

STRATEGIES: dict[
    str, type[IntradayOrbVwap] | type[SwingTrendPullback] | type[LongTermQualityValue]
] = {
    IntradayOrbVwap.name: IntradayOrbVwap,
    SwingTrendPullback.name: SwingTrendPullback,
    LongTermQualityValue.name: LongTermQualityValue,
}


def get_strategy(name: str) -> Strategy:
    return STRATEGIES[name]()
