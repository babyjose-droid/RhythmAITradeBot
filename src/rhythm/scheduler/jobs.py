"""What each job kind does. Every kind reuses the same service code as the API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from rhythm.api.state import AppState
from rhythm.audit import ActorType, AuditLog
from rhythm.controls import service as controls
from rhythm.core.errors import RhythmError
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.events import store as event_store
from rhythm.fundamentals import store as fundamentals_store
from rhythm.instruments import watchlists as wl_svc
from rhythm.monitoring import health
from rhythm.notifications.service import notify
from rhythm.paper import service as paper
from rhythm.strategies.registry import STRATEGIES

ACTOR = "scheduler"


class JobConfigError(RhythmError):
    """The job's parameters are invalid (it cannot succeed until edited)."""


def _keys(session: Session, watchlist_id: Any) -> list[InstrumentKey]:
    try:
        wl = wl_svc.get_watchlist(session, int(watchlist_id))
    except (wl_svc.NotFoundError, TypeError, ValueError) as exc:
        raise JobConfigError(f"watchlist {watchlist_id!r} not found") from exc
    return [InstrumentKey(Exchange(i.exchange), i.symbol) for i in wl.items]


def validate_params(session: Session, kind: str, params: dict[str, Any]) -> None:
    if kind == "paper_cycle":
        name = params.get("strategy")
        if name not in STRATEGIES:
            raise JobConfigError(f"unknown strategy {name!r}")
        try:
            paper.get_account(session, int(params.get("account_id", 0)))
        except (paper.AccountNotFoundError, TypeError, ValueError) as exc:
            raise JobConfigError(f"PAPER account {params.get('account_id')!r} not found") from exc
        _keys(session, params.get("watchlist_id"))
    elif kind in ("events_sync", "fundamentals_sync"):
        _keys(session, params.get("watchlist_id"))
    elif kind in ("health_check", "audit_verify"):
        if params:
            raise JobConfigError(f"{kind} takes no parameters")
    else:
        raise JobConfigError(f"unknown job kind {kind!r}")


def run(
    session: Session, state: AppState, kind: str, params: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """Execute one job inside the caller's transaction. Raises on failure (caller rolls back)."""
    if kind == "paper_cycle":
        if state.market_data is None:
            raise JobConfigError("no market data provider configured")
        strategy = STRATEGIES[params["strategy"]]()
        res = paper.run_cycle(
            session,
            keys=_keys(session, params["watchlist_id"]),
            provider=state.market_data,
            strategy=strategy,
            settings=state.settings,
            clock=state.clock,
            calendar=state.calendar,
            audit=state.audit,
            actor_id=ACTOR,
            account_id=int(params["account_id"]),
            policy=state.freshness.policy,
            research=state.research,
        )
        return {
            "horizon": res.horizon,
            "new_orders": res.new_orders,
            "new_fills": res.new_fills,
            "signals": len(res.signals),
            "skipped": res.skipped,
            "auto_paused": res.auto_paused,
            "data_is_mock": res.data_is_mock,
        }
    if kind == "events_sync":
        if state.events is None:
            raise JobConfigError("no event provider configured")
        keys = _keys(session, params["watchlist_id"])
        added = event_store.sync(
            session,
            state.events,
            keys,
            now=now,
            environment=state.settings.environment,
            audit=state.audit,
            actor_id=ACTOR,
        )
        return {"added": added, "instruments": len(keys)}
    if kind == "fundamentals_sync":
        if state.fundamentals is None:
            raise JobConfigError("no fundamentals provider configured")
        keys = _keys(session, params["watchlist_id"])
        added = fundamentals_store.sync(
            session,
            state.fundamentals,
            keys,
            now=now,
            environment=state.settings.environment,
            audit=state.audit,
            actor_id=ACTOR,
        )
        return {"added": added, "instruments": len(keys)}
    if kind == "health_check":
        statuses = health.evaluate(session, state.settings, now)
        changed = health.record_transitions(session, statuses, now)
        return {"components": {s.component: s.status for s in statuses}, "changed": changed}
    if kind == "audit_verify":
        ver = AuditLog.verify(session)
        if not ver.ok:
            notify(
                session,
                kind="audit_chain_broken",
                severity="critical",
                title="Audit chain verification FAILED — kill switch activated",
                body=f"first bad row {ver.first_bad_id}: {ver.reason}",
                at=now,
                link="/audit",
            )
            controls.set_kill_switch(
                session,
                state.audit,
                active=True,
                actor_type=ActorType.SYSTEM,
                actor_id=ACTOR,
                reason=f"audit chain verification failed at row {ver.first_bad_id}",
                at=now,
            )
        return {"ok": ver.ok, "rows_checked": ver.rows_checked, "first_bad_id": ver.first_bad_id}
    raise JobConfigError(f"unknown job kind {kind!r}")
