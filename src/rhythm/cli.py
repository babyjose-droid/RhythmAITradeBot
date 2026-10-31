"""Operator CLI: ``rhythm-cli <command>``.

Phase 2 commands work with the MOCK sources only; every output says MOCK. Mock data is
refused outside local/test environments by the same guards the API uses.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import timedelta

from rhythm.audit import AuditLog
from rhythm.broker.mock import MOCK_SYMBOLS, MockBrokerAdapter
from rhythm.config import get_settings
from rhythm.core.calendar import SessionCalendar
from rhythm.core.clock import SystemClock, to_exchange_local
from rhythm.core.logging import configure_logging
from rhythm.core.market import DataOrigin, Exchange, InstrumentKey
from rhythm.db.session import create_db_engine, create_session_factory, session_scope
from rhythm.instruments import watchlists as wl_svc
from rhythm.instruments.registry import MockDataNotAllowedError, apply_snapshot
from rhythm.instruments.snapshot import SnapshotRejectedError
from rhythm.marketdata.bars import upsert_bars
from rhythm.marketdata.mock import MockMarketDataProvider
from rhythm.marketdata.types import Interval


def _print(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rhythm-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("instruments-sync", help="apply a scrip-master snapshot (mock source)")
    p_sync.add_argument("--source", choices=["mock"], required=True)
    p_sync.add_argument("--exchange", default="NSE", choices=[e.value for e in Exchange])

    p_bars = sub.add_parser("bars-backfill", help="store bars from a provider (mock source)")
    p_bars.add_argument("--source", choices=["mock"], required=True)
    p_bars.add_argument("--instrument", required=True, help="EXCHANGE:SYMBOL")
    p_bars.add_argument("--interval", default="1d", choices=[i.value for i in Interval])
    p_bars.add_argument("--days", type=int, default=30)

    p_seed = sub.add_parser("watchlist-seed-mock", help="create a demo watchlist of MOCK names")
    p_seed.add_argument("--name", default="Mock intraday demo")
    p_seed.add_argument("--horizon", default="INTRADAY", choices=list(wl_svc.WATCHLIST_HORIZONS))
    p_seed.add_argument("--count", type=int, default=5)

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except MockDataNotAllowedError as exc:
        _print({"status": "REFUSED", "reason": str(exc)})
        return 3


def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    clock = SystemClock()
    engine = create_db_engine(settings.database_url.get_secret_value())
    factory = create_session_factory(engine)
    audit = AuditLog(environment=settings.environment.value, clock=clock)
    actor = settings.local_actor_id

    if args.cmd == "instruments-sync":
        exchange = Exchange(args.exchange)
        records = MockBrokerAdapter(clock=clock).get_scrip_master(exchange)
        with session_scope(factory) as s:
            try:
                res = apply_snapshot(
                    s,
                    records=records,
                    exchange=exchange,
                    source=DataOrigin.MOCK,
                    as_of=to_exchange_local(clock.now()).date(),
                    clock=clock,
                    audit=audit,
                    environment=settings.environment,
                    actor_id=actor,
                )
            except SnapshotRejectedError as exc:
                _print({"status": "REJECTED", "reason": exc.reason, "source": "MOCK"})
                return 2
        _print(
            {"source": "MOCK", "snapshot_id": res.snapshot_id, "status": res.status, **res.summary}
        )
        return 0

    if args.cmd == "bars-backfill":
        key = InstrumentKey.parse(args.instrument)
        provider = MockMarketDataProvider(clock=clock, calendar=SessionCalendar())
        end = clock.now()
        bars = provider.bars(key, Interval(args.interval), end - timedelta(days=args.days), end)
        with session_scope(factory) as s:
            up = upsert_bars(s, bars, clock=clock, environment=settings.environment)
        _print(
            {
                "source": "MOCK",
                "instrument": str(key),
                "written": up.written,
                "rejected": up.rejected,
            }
        )
        return 0

    # watchlist-seed-mock
    with session_scope(factory) as s:
        wl = wl_svc.create_watchlist(
            s,
            audit,
            name=args.name,
            horizon=args.horizon,
            description="Synthetic MOCK instruments for local testing only",
            actor_id=actor,
        )
        for sym in MOCK_SYMBOLS[: args.count]:
            wl_svc.add_item(
                s,
                audit,
                watchlist_id=wl.id,
                key=InstrumentKey(Exchange.NSE, sym),
                note="MOCK",
                actor_id=actor,
            )
        _print({"watchlist_id": wl.id, "name": wl.name, "items": list(MOCK_SYMBOLS[: args.count])})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
