"""Persist backtest runs (configuration, provenance, metrics, trades) for reproducibility."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.orm import Session

from rhythm.audit import ActorType, AuditLog
from rhythm.backtest.engine import BacktestConfig, BacktestResult
from rhythm.db.models import BacktestRun, BacktestTrade
from rhythm.strategies.base import Strategy, params_hash

FULL = "full"


def save_run(
    session: Session,
    *,
    strategy: Strategy,
    cfg: BacktestConfig,
    results: Mapping[str, BacktestResult],
    audit: AuditLog,
    actor_id: str,
) -> BacktestRun:
    """``results`` must contain the ``full`` run; other keys are named splits."""
    full = results[FULL]
    run = BacktestRun(
        strategy=strategy.name,
        version=strategy.version,
        params=strategy.params,
        params_hash=params_hash(strategy.params),
        symbols=[str(k) for k in cfg.symbols],
        interval=cfg.interval.value,
        start_at=cfg.start,
        end_at=cfg.end,
        initial_capital=cfg.initial_capital,
        cost_model=cfg.cost_model.describe(),
        fill_model=cfg.fill_model.describe(),
        limits=cfg.describe_risk(),
        data_hash=full.data_hash,
        run_hash=full.run_hash,
        data_is_mock=full.data_is_mock,
        bars_processed=full.bars_processed,
        signals_count=len(full.signals),
        metrics=full.metrics,
        splits={
            name: {"metrics": r.metrics, "run_hash": r.run_hash, "data_hash": r.data_hash}
            for name, r in results.items()
            if name != FULL
        },
    )
    session.add(run)
    session.flush()
    for name, res in results.items():
        for seq, t in enumerate(res.trades):
            session.add(
                BacktestTrade(
                    run_id=run.id,
                    split=name,
                    seq=seq,
                    exchange=t.key.exchange.value,
                    symbol=t.key.symbol,
                    entry_at=t.entry_at,
                    exit_at=t.exit_at,
                    quantity=t.quantity,
                    entry_price=t.entry_price,
                    exit_price=t.exit_price,
                    gross_pnl=t.gross_pnl,
                    charges=t.charges,
                    net_pnl=t.net_pnl,
                    r_multiple=t.r_multiple,
                    exit_reason=t.exit_reason,
                )
            )
    session.flush()
    audit.append(
        session,
        event_type="backtest.completed",
        actor_type=ActorType.USER,
        actor_id=actor_id,
        subject_type="backtest_run",
        subject_id=str(run.id),
        payload={
            "strategy": f"{strategy.name}@{strategy.version}",
            "run_hash": full.run_hash,
            "data_is_mock": full.data_is_mock,
            "trades": len(full.trades),
        },
    )
    return run
