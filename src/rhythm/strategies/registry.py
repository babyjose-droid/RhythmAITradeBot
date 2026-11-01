"""All available strategies by name. Adding one here exposes it to backtests and cycles."""

from __future__ import annotations

from rhythm.strategies.base import Strategy
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from rhythm.strategies.swing_trend import SwingTrendPullback

STRATEGIES: dict[str, type[IntradayOrbVwap] | type[SwingTrendPullback]] = {
    IntradayOrbVwap.name: IntradayOrbVwap,
    SwingTrendPullback.name: SwingTrendPullback,
}


def get_strategy(name: str) -> Strategy:
    return STRATEGIES[name]()
