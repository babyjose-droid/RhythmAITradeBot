"""Event-driven, chronological backtester for intraday strategies.

Reuses the exact runner, PAPER book, fill model, cost model and risk gate used by paper
trading. Reproducible: the same inputs always produce the same ``run_hash`` and results.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import EXCHANGE_TZ, ensure_utc
from rhythm.core.market import InstrumentKey
from rhythm.execution.book import PaperBook
from rhythm.execution.costs import CostModel, Side
from rhythm.execution.fills import FillModel
from rhythm.execution.runner import IntradayRunner, RiskEnvironment, SignalRecord
from rhythm.marketdata.types import Bar, Interval
from rhythm.risk.engine import InstrumentFacts
from rhythm.risk.policy import DEFAULT_POLICY, RiskPolicy
from rhythm.strategies.base import Strategy, params_hash


class BacktestInputError(ValueError):
    pass


@dataclass(frozen=True)
class Split:
    name: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class BacktestConfig:
    symbols: tuple[InstrumentKey, ...]
    start: datetime
    end: datetime
    interval: Interval = Interval.M5
    initial_capital: Decimal = Decimal(1_000_000)
    cost_model: CostModel = field(default_factory=CostModel)
    fill_model: FillModel = field(default_factory=FillModel)
    policy: RiskPolicy = DEFAULT_POLICY
    # Share of capital the intraday ledger may use. 1 = test the strategy in isolation.
    ledger_allocation_pct: Decimal = Decimal(1)
    # Instrument facts for R5/R6. Missing symbols get MOCK-style defaults (EQ, lot 1, tick 0.05).
    instruments: Mapping[InstrumentKey, InstrumentFacts] = field(default_factory=dict)

    def facts(self, key: InstrumentKey) -> InstrumentFacts:
        return self.instruments.get(key) or InstrumentFacts(
            active=True, instrument_type="EQ", lot_size=1, tick_size=Decimal("0.05")
        )

    def describe_risk(self) -> dict[str, Any]:
        return {
            "policy": self.policy.describe(),
            "ledger_allocation_pct": str(self.ledger_allocation_pct),
        }


@dataclass(frozen=True)
class Trade:
    key: InstrumentKey
    entry_at: datetime
    exit_at: datetime
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    gross_pnl: Decimal
    charges: Decimal
    net_pnl: Decimal
    r_multiple: float | None
    exit_reason: str


@dataclass
class BacktestResult:
    run_hash: str
    data_hash: str
    data_is_mock: bool
    metrics: dict[str, Any]
    trades: list[Trade]
    equity_curve: list[tuple[datetime, Decimal]]
    signals: list[SignalRecord]
    bars_processed: int


def data_hash(bars: Mapping[InstrumentKey, Sequence[Bar]]) -> str:
    h = hashlib.sha256()
    for key in sorted(bars):
        for b in bars[key]:
            h.update(
                f"{key}|{b.interval.value}|{b.start.isoformat()}|{b.open}|{b.high}|{b.low}|"
                f"{b.close}|{b.volume}|{b.source.value}\n".encode()
            )
    return h.hexdigest()


def run_hash(strategy: Strategy, cfg: BacktestConfig, dhash: str) -> str:
    material = {
        "strategy": strategy.name,
        "version": strategy.version,
        "params": strategy.params,
        "params_hash": params_hash(strategy.params),
        "symbols": [str(k) for k in cfg.symbols],
        "start": cfg.start.isoformat(),
        "end": cfg.end.isoformat(),
        "interval": cfg.interval.value,
        "initial_capital": str(cfg.initial_capital),
        "cost_model": cfg.cost_model.describe(),
        "fill_model": cfg.fill_model.describe(),
        "risk": cfg.describe_risk(),
        "data_hash": dhash,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _validate(cfg: BacktestConfig, bars: Mapping[InstrumentKey, Sequence[Bar]]) -> None:
    if cfg.end <= cfg.start:
        raise BacktestInputError("end must be after start")
    for key in cfg.symbols:
        prev = None
        for b in bars.get(key, ()):
            if b.key != key or b.interval is not cfg.interval:
                raise BacktestInputError(f"unexpected bar for {key}: {b.key} {b.interval}")
            if prev is not None and b.start <= prev:
                raise BacktestInputError(f"{key}: bars not strictly increasing")
            prev = b.start


def run_backtest(
    strategy: Strategy,
    cfg: BacktestConfig,
    bars: Mapping[InstrumentKey, Sequence[Bar]],
    *,
    calendar: SessionCalendar | None = None,
) -> BacktestResult:
    calendar = calendar or SessionCalendar()
    start, end = ensure_utc(cfg.start), ensure_utc(cfg.end)
    window = {k: [b for b in bars.get(k, ()) if start <= b.start < end] for k in cfg.symbols}
    _validate(cfg, window)
    dhash = data_hash(window)
    book = PaperBook(cash=cfg.initial_capital, cost_model=cfg.cost_model, fill_model=cfg.fill_model)
    env = RiskEnvironment(
        mode="BACKTEST",
        policy=cfg.policy,
        book_is_truth=True,
        ledger_allocation=cfg.initial_capital * cfg.ledger_allocation_pct,
        instruments={k: cfg.facts(k) for k in cfg.symbols},
    )
    runner = IntradayRunner(strategy=strategy, book=book, calendar=calendar, env=env)

    timeline = sorted((b.start, str(b.key), b) for k in cfg.symbols for b in window[k])
    equity_curve: list[tuple[datetime, Decimal]] = []
    signals: list[SignalRecord] = []
    exposed_steps = 0
    steps = 0
    i = 0
    while i < len(timeline):
        ts = timeline[i][0]
        while i < len(timeline) and timeline[i][0] == ts:
            _, recs = runner.on_bar(timeline[i][2])
            signals.extend(recs)
            i += 1
        steps += 1
        if any(p.quantity > 0 for p in book.positions.values()):
            exposed_steps += 1
        equity_curve.append((ts, book.equity(runner.marks)))

    trades = _trades(book, signals)
    metrics = compute_metrics(
        initial=cfg.initial_capital,
        equity_curve=equity_curve,
        trades=trades,
        book=book,
        window=window,
        exposure=(exposed_steps / steps) if steps else 0.0,
    )
    return BacktestResult(
        run_hash=run_hash(strategy, cfg, dhash),
        data_hash=dhash,
        data_is_mock=any(b.is_mock for bs in window.values() for b in bs),
        metrics=metrics,
        trades=trades,
        equity_curve=equity_curve,
        signals=signals,
        bars_processed=len(timeline),
    )


def run_splits(
    strategy: Strategy,
    cfg: BacktestConfig,
    bars: Mapping[InstrumentKey, Sequence[Bar]],
    splits: Sequence[Split],
    *,
    calendar: SessionCalendar | None = None,
) -> dict[str, BacktestResult]:
    """Independent runs per period (e.g. development / validation / out-of-sample).

    Parameters are fixed across splits: nothing here optimises on later periods.
    """
    names = [s.name for s in splits]
    if len(set(names)) != len(names):
        raise BacktestInputError("split names must be unique")
    ordered = sorted(splits, key=lambda s: s.start)
    for a, b in itertools.pairwise(ordered):
        if b.start < a.end:
            raise BacktestInputError(f"splits {a.name} and {b.name} overlap")
    out: dict[str, BacktestResult] = {}
    for s in splits:
        sub = BacktestConfig(
            symbols=cfg.symbols,
            start=s.start,
            end=s.end,
            interval=cfg.interval,
            initial_capital=cfg.initial_capital,
            cost_model=cfg.cost_model,
            fill_model=cfg.fill_model,
            policy=cfg.policy,
            ledger_allocation_pct=cfg.ledger_allocation_pct,
            instruments=cfg.instruments,
        )
        out[s.name] = run_backtest(strategy, sub, bars, calendar=calendar)
    return out


def _trades(book: PaperBook, signals: Sequence[SignalRecord]) -> list[Trade]:
    stops = {r.order_id: r.signal.stop for r in signals if r.order_id}
    out: list[Trade] = []
    open_: dict[InstrumentKey, dict[str, Any]] = {}
    for f in book.fills:
        order = book.orders[f.order_id]
        cur = open_.get(f.key)
        if f.side is Side.BUY:
            if cur is None:
                cur = open_[f.key] = {
                    "entry_at": f.at,
                    "qty": 0,
                    "cost": Decimal(0),
                    "charges": Decimal(0),
                    "stop": stops.get(order.id),
                    "sold": 0,
                    "proceeds": Decimal(0),
                }
            cur["qty"] += f.quantity
            cur["cost"] += f.price * f.quantity
            cur["charges"] += f.charges.total
            continue
        if cur is None:
            continue
        cur["sold"] += f.quantity
        cur["proceeds"] += f.price * f.quantity
        cur["charges"] += f.charges.total
        if cur["sold"] >= cur["qty"]:
            qty = cur["qty"]
            entry = cur["cost"] / qty
            exit_ = cur["proceeds"] / qty
            gross = cur["proceeds"] - cur["cost"]
            net = gross - cur["charges"]
            stop = cur["stop"]
            risk = (entry - stop) * qty if stop is not None and entry > stop else None
            out.append(
                Trade(
                    key=f.key,
                    entry_at=cur["entry_at"],
                    exit_at=f.at,
                    quantity=qty,
                    entry_price=entry.quantize(Decimal("0.0001")),
                    exit_price=exit_.quantize(Decimal("0.0001")),
                    gross_pnl=gross.quantize(Decimal("0.01")),
                    charges=cur["charges"],
                    net_pnl=net.quantize(Decimal("0.01")),
                    r_multiple=None if risk is None else round(float(net / risk), 3),
                    exit_reason=order.purpose,
                )
            )
            del open_[f.key]
    return out


def compute_metrics(
    *,
    initial: Decimal,
    equity_curve: Sequence[tuple[datetime, Decimal]],
    trades: Sequence[Trade],
    book: PaperBook,
    window: Mapping[InstrumentKey, Sequence[Bar]],
    exposure: float,
) -> dict[str, Any]:
    final = equity_curve[-1][1] if equity_curve else initial
    peak, max_dd = initial, Decimal(0)
    daily: dict[date, Decimal] = {}
    for ts, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
        daily[ts.astimezone(EXCHANGE_TZ).date()] = eq
    closes = [initial, *daily.values()]
    rets = [float(b / a - 1) for a, b in itertools.pairwise(closes) if a > 0]
    vol = statistics.stdev(rets) * math.sqrt(252) if len(rets) >= 2 else None
    sharpe = (
        statistics.mean(rets) / statistics.stdev(rets) * math.sqrt(252)
        if len(rets) >= 2 and statistics.stdev(rets) > 0
        else None
    )
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    traded_value = sum((f.price * f.quantity for f in book.fills), Decimal(0))
    bench = []
    for bs in window.values():
        if bs:
            bench.append(float(bs[-1].close / bs[0].open - 1))
    return {
        "start_equity": str(initial),
        "end_equity": str(final.quantize(Decimal("0.01"))),
        "total_return_pct": round(float(final / initial - 1) * 100, 4),
        "max_drawdown_pct": round(float(max_dd) * 100, 4),
        "annualised_volatility_pct": None if vol is None else round(vol * 100, 4),
        "sharpe_rf0": None if sharpe is None else round(sharpe, 3),
        "days": len(daily),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "expectancy_per_trade": str((sum(nets, Decimal(0)) / len(nets)).quantize(Decimal("0.01")))
        if nets
        else None,
        "profit_factor": round(float(sum(wins) / -sum(losses)), 3)
        if wins and losses and sum(losses) < 0
        else None,
        "average_r": round(statistics.mean(rs), 3) if rs else None,
        "total_charges": str(sum((f.charges.total for f in book.fills), Decimal(0))),
        "turnover_x": round(float(traded_value / initial), 4) if initial else None,
        "exposure_pct": round(exposure * 100, 2),
        "benchmark_equal_weight_buy_hold_pct": round(statistics.mean(bench) * 100, 4)
        if bench
        else None,
        "exit_reasons": dict(_count(t.exit_reason for t in trades)),
    }


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for i in items:
        out[i] += 1
    return out
