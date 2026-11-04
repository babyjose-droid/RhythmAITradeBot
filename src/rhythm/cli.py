"""Operator CLI: ``rhythm-cli <command>``.

Phase 2 commands work with the MOCK sources only; every output says MOCK. Mock data is
refused outside local/test environments by the same guards the API uses.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from datetime import timedelta

from rhythm.audit import AuditLog
from rhythm.auth.service import AuthService, UserExistsError
from rhythm.broker.mock import MOCK_SYMBOLS, MockBrokerAdapter
from rhythm.config import Settings, get_settings
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


def _read_password() -> str:
    """From RHYTHM_NEW_USER_PASSWORD (non-interactive) or a hidden prompt (asked twice)."""
    env = os.environ.get("RHYTHM_NEW_USER_PASSWORD")
    if env:
        return env
    first = getpass.getpass("New password (min 12 characters): ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("passwords do not match")
    return first


def _users(args: argparse.Namespace, settings: Settings) -> int:
    if not settings.auth.enabled:
        sys.stderr.write("authentication is disabled (RHYTHM_AUTH__ENABLED=false)\n")
        return 2
    clock = SystemClock()
    svc = AuthService(settings.auth, AuditLog(environment=settings.environment.value, clock=clock))
    engine = create_db_engine(settings.database_url.get_secret_value())
    with session_scope(create_session_factory(engine)) as s:
        if args.cmd == "user-enable":
            svc.set_disabled(s, args.username, disabled=False, actor="cli", at=clock.now())
            _print({"username": args.username, "enabled": True})
            return 0
        if args.cmd == "user-reset-mfa":
            e = svc.reset_mfa(s, args.username, actor="cli", at=clock.now())
        else:
            try:
                e = svc.create_user(
                    s,
                    username=args.username,
                    password=_read_password(),
                    role=args.role,
                    created_by="cli",
                    at=clock.now(),
                )
            except (ValueError, UserExistsError) as exc:
                sys.stderr.write(f"error: {exc}\n")
                return 2
    sys.stdout.write(
        f"User {e.username} ({e.role}). Add this to your authenticator app now"
        " (it is shown once):\n"
        f"  secret: {e.totp_secret}\n  uri:    {e.provisioning_uri}\n"
        "Then sign in at the dashboard with the password and the 6-digit code.\n"
    )
    return 0


def _restore_verify(url: str) -> int:
    """Used by the restore drill on a scratch copy: never point it at production writes."""
    from sqlalchemy import func, select  # noqa: PLC0415 — only this command needs them

    from rhythm.db.migrate import current_revision, head_revision  # noqa: PLC0415
    from rhythm.db.models import AuditEvent, OrderRow, PaperAccount  # noqa: PLC0415

    engine = create_db_engine(url)
    try:
        cur, head = current_revision(engine), head_revision()
        with session_scope(create_session_factory(engine)) as s:
            ver = AuditLog.verify(s)
            counts = {
                "audit_events": s.execute(
                    select(func.count()).select_from(AuditEvent)
                ).scalar_one(),
                "paper_accounts": s.execute(
                    select(func.count()).select_from(PaperAccount)
                ).scalar_one(),
                "orders": s.execute(select(func.count()).select_from(OrderRow)).scalar_one(),
            }
    finally:
        engine.dispose()
    ok = cur == head and ver.ok
    _print(
        {
            "ok": ok,
            "schema": {"current": cur, "head": head},
            "audit_chain": {
                "ok": ver.ok,
                "rows": ver.rows_checked,
                "first_bad_id": ver.first_bad_id,
            },
            "counts": counts,
        }
    )
    return 0 if ok else 1


def _scheduler_health() -> int:
    from datetime import UTC, datetime  # noqa: PLC0415

    from rhythm.monitoring.health import leader_heartbeat_age  # noqa: PLC0415

    settings = get_settings()
    engine = create_db_engine(settings.database_url.get_secret_value())
    try:
        with session_scope(create_session_factory(engine)) as s:
            age = leader_heartbeat_age(s, datetime.now(UTC))
    finally:
        engine.dispose()
    ok = age is not None and age <= settings.scheduler.heartbeat_stale_seconds
    _print({"ok": ok, "leader_heartbeat_age_seconds": age})
    return 0 if ok else 1


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

    p_bt = sub.add_parser(
        "backtest-run", help="backtest a strategy on MOCK bars (5m intraday, daily swing)"
    )
    p_bt.add_argument(
        "--strategy",
        default="intraday_orb_vwap",
        choices=["intraday_orb_vwap", "swing_trend_pullback", "long_term_quality_value"],
    )
    p_bt.add_argument("--symbols", default="NSE:MOCKA,NSE:MOCKB,NSE:MOCKC")
    p_bt.add_argument("--days", type=int, default=30)
    p_bt.add_argument("--capital", default="1000000")

    p_rv = sub.add_parser(
        "restore-verify", help="check a restored database: schema at head, audit chain intact"
    )
    target = p_rv.add_mutually_exclusive_group(required=True)
    target.add_argument("--database-url")
    target.add_argument(
        "--database-name", help="same server and credentials as RHYTHM_DATABASE_URL, other database"
    )
    sub.add_parser("scheduler-health", help="exit 0 if a leader scheduler beat recently")

    p_user = sub.add_parser(
        "user-create", help="create a dashboard user (password + authenticator app)"
    )
    p_user.add_argument("--username", required=True)
    p_user.add_argument("--role", choices=["owner", "approver", "viewer"], required=True)
    p_mfa = sub.add_parser("user-reset-mfa", help="issue a new authenticator secret for a user")
    p_mfa.add_argument("--username", required=True)
    p_unlock = sub.add_parser("user-enable", help="re-enable / unlock a user")
    p_unlock.add_argument("--username", required=True)

    p_acct = sub.add_parser("paper-account-create", help="create a PAPER account")
    p_acct.add_argument("--name", required=True)
    p_acct.add_argument("--cash", default="1000000")

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

    if args.cmd == "backtest-run":
        from decimal import Decimal  # noqa: PLC0415

        from rhythm.backtest.engine import (  # noqa: PLC0415
            BacktestConfig,
            expected_interval,
            run_backtest,
        )
        from rhythm.backtest.store import FULL, save_run  # noqa: PLC0415
        from rhythm.core.market import Horizon  # noqa: PLC0415
        from rhythm.events.mock import MockEventProvider  # noqa: PLC0415
        from rhythm.fundamentals.mock import MockFundamentalsProvider  # noqa: PLC0415
        from rhythm.fundamentals.store import HISTORY_START  # noqa: PLC0415
        from rhythm.strategies.registry import get_strategy  # noqa: PLC0415

        keys = tuple(InstrumentKey.parse(x) for x in args.symbols.split(","))
        cal = SessionCalendar()
        provider = MockMarketDataProvider(clock=clock, calendar=cal)
        end = clock.now()
        start = end - timedelta(days=args.days)
        strat = get_strategy(args.strategy)
        interval = expected_interval(strat)
        bt_bars = {k: list(provider.bars(k, interval, start, end)) for k in keys}
        events = {}
        fundamentals = {}
        if strat.horizon is not Horizon.INTRADAY:
            ev = MockEventProvider()
            horizon_end = end.date() + timedelta(days=60)
            events = {k: tuple(ev.events(k, start.date(), horizon_end)) for k in keys}
        if strat.horizon is Horizon.LONG_TERM:
            fp = MockFundamentalsProvider(provider.price_at)
            fundamentals = {k: tuple(fp.snapshots(k, HISTORY_START, end.date())) for k in keys}
        cfg = BacktestConfig(
            symbols=keys,
            start=start,
            end=end,
            interval=interval,
            initial_capital=Decimal(args.capital),
            events=events,
            fundamentals=fundamentals,
        )
        bt = run_backtest(strat, cfg, bt_bars, calendar=cal)
        with session_scope(factory) as s:
            run = save_run(
                s, strategy=strat, cfg=cfg, results={FULL: bt}, audit=audit, actor_id=actor
            )
            run_id = run.id
        _print({"source": "MOCK", "run_id": run_id, "run_hash": bt.run_hash, **bt.metrics})
        return 0

    if args.cmd == "restore-verify":
        url = args.database_url
        if url is None:
            if "drill" not in args.database_name:
                sys.stderr.write("refusing: --database-name must contain 'drill'\n")
                return 2
            base = settings.database_url.get_secret_value()
            url = base.rsplit("/", 1)[0] + "/" + args.database_name
        return _restore_verify(url)
    if args.cmd == "scheduler-health":
        return _scheduler_health()

    if args.cmd in ("user-create", "user-reset-mfa", "user-enable"):
        return _users(args, settings)

    if args.cmd == "paper-account-create":
        from decimal import Decimal  # noqa: PLC0415

        from rhythm.paper.service import create_account  # noqa: PLC0415

        with session_scope(factory) as s:
            acct = create_account(
                s, audit, name=args.name, starting_cash=Decimal(args.cash), actor_id=actor
            )
            _print(
                {"account_kind": "PAPER", "id": acct.id, "name": acct.name, "cash": str(acct.cash)}
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
