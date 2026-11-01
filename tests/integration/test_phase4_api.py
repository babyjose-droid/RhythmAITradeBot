"""Phase 4 end to end on PostgreSQL: risk engine persistence, controls, ledgers, reconciliation,
idempotency and UNKNOWN orders. MOCK data only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError

from rhythm.core.clock import EXCHANGE_TZ
from rhythm.core.market import Exchange, InstrumentKey
from rhythm.db.models import AuditEvent, ControlEventRow, OrderRow, PaperAccount, RiskCheckRow
from rhythm.db.session import create_session_factory, session_scope
from rhythm.execution.gateway import FlakyVenue
from rhythm.instruments import watchlists as wl_svc
from rhythm.paper import service as paper
from rhythm.strategies.intraday_orb import IntradayOrbVwap
from tests.integration.test_phase3_api import KEY, SIGNAL_AT, Env

pytestmark = pytest.mark.integration
STRAT = IntradayOrbVwap().name


@pytest.fixture
def env4(db_url: str, migrated: Engine) -> Iterator[Env]:
    e = Env(db_url, migrated, paper=True)
    with e.c:
        yield e


def _setup(env: Env, name: str = "p4") -> tuple[int, int]:
    wid = env.setup_watchlist()
    aid = env.c.post("/v1/paper/accounts", json={"name": name}).json()["id"]
    return wid, aid


def _cycle(env: Env, aid: int, wid: int, **headers: str) -> Any:
    return env.c.post(
        f"/v1/paper/accounts/{aid}/cycle", json={"watchlist_id": wid}, headers=headers
    )


def _sf(env: Env) -> Any:
    return create_session_factory(env.engine)


# ---------------------------------------------------------------------------- ledgers
def test_ledgers_created_and_allocation_change_is_audited(env4: Env) -> None:
    c = env4.c
    _, aid = _setup(env4)
    led = c.get(f"/v1/portfolio/ledgers?account_id={aid}")
    assert led.headers["x-account-kind"] == "PAPER"
    alloc = {lg["horizon"]: Decimal(lg["allocated_capital"]) for lg in led.json()["ledgers"]}
    assert alloc == {
        "INTRADAY": Decimal(300_000),
        "SWING": Decimal(300_000),
        "LONG_TERM": Decimal(400_000),
    }
    url = f"/v1/paper/accounts/{aid}/ledgers/INTRADAY"
    body = {"allocated_capital": "250000", "reason": "smaller intraday book"}
    assert c.put(url, json=body, headers={"Idempotency-Key": ""}).status_code in (422, 428)
    ok = c.put(url, json=body)
    assert ok.status_code == 200 and Decimal(ok.json()["allocated_capital"]) == 250_000
    too_much = c.put(url, json={"allocated_capital": "700000", "reason": "over-allocate"})
    assert too_much.status_code == 422
    assert c.get(f"/v1/paper/accounts/{aid}").json()["ledgers"]["INTRADAY"] == "250000.0000"
    with session_scope(_sf(env4)) as s:
        assert (
            s.execute(
                select(func.count()).where(AuditEvent.event_type == "portfolio.allocation_changed")
            ).scalar_one()
            == 1
        )


# ---------------------------------------------------------------------------- idempotency
def test_idempotency_key_required_replayed_and_conflicting(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    url = f"/v1/paper/accounts/{aid}/cycle"
    c.event_hooks = {"request": [], "response": []}  # no automatic keys in this test
    assert c.post(url, json={"watchlist_id": wid}).status_code == 428
    key = {"Idempotency-Key": "cycle-0001-abc"}
    first = c.post(url, json={"watchlist_id": wid}, headers=key)
    assert first.status_code == 200 and first.json()["new_orders"] == 1
    replay = c.post(url, json={"watchlist_id": wid}, headers=key)
    assert replay.headers["idempotent-replay"] == "true"
    assert replay.json() == first.json()
    with session_scope(_sf(env4)) as s:
        assert s.execute(select(func.count()).select_from(OrderRow)).scalar_one() == 1
        cycles = s.execute(
            select(func.count()).where(AuditEvent.event_type == "paper.cycle")
        ).scalar_one()
        assert cycles == 1  # the replay did not run the cycle again
    other = c.post(url, json={"watchlist_id": wid, "strategy": "x"}, headers=key)
    assert other.status_code == 409


# ---------------------------------------------------------------------------- risk decisions
def test_every_decision_is_persisted_with_all_checks(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    r = _cycle(env4, aid, wid).json()
    (order,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    checks = c.get(f"/v1/risk/checks?account_id={aid}").json()
    assert len(checks) == r["risk_decisions"] >= 1
    entry = next(x for x in checks if x["order_id"] == order["id"])
    assert entry["outcome"] in ("ACCEPT", "REDUCE") and entry["quantity"] == order["quantity"]
    assert {x["code"] for x in entry["checks"]} >= {f"R{i}" for i in range(1, 20)}
    assert entry["inputs"]["mode"] == "PAPER" and entry["inputs"]["quote_status"] == "FRESH"
    with pytest.raises(DBAPIError, match="append-only"), session_scope(_sf(env4)) as s:
        s.execute(text("UPDATE risk_checks SET outcome = 'ACCEPT'"))


def test_policy_versioning(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    before = c.get("/v1/risk/policies").json()
    assert before["active"]["version"] == "paper-default-2" and before["history"] == []
    bad = c.post(
        "/v1/risk/policies",
        json={"params": {"intraday_risk_per_trade_pct": "0.9"}, "reason": "too big"},
    )
    assert bad.status_code == 422
    new = c.post(
        "/v1/risk/policies",
        json={"params": {"intraday_risk_per_trade_pct": "0.001"}, "reason": "tighter paper test"},
    )
    assert new.status_code == 201 and new.json()["version"] == "paper-2"
    assert c.get("/v1/risk/policies").json()["active"]["intraday_risk_per_trade_pct"] == "0.001"
    _cycle(env4, aid, wid)
    (row,) = c.get(f"/v1/risk/checks?account_id={aid}").json()
    assert row["policy_version"] == "paper-2"


# ---------------------------------------------------------------------------- kill switch
def test_kill_switch_cancels_entries_blocks_new_and_needs_reason_to_release(
    env4: Env,
) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    _cycle(env4, aid, wid)
    (order,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert order["state"] == "BROKER_ACKNOWLEDGED"

    on = c.post("/v1/risk/kill-switch", json={"action": "activate", "reason": "drill"}).json()
    assert on["kill_switch_active"] is True and on["cancelled_orders"] == [order["id"]]
    (after,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert after["state"] == "CANCELLED" and after["reason"] == "kill switch"

    # a research cycle shows the block explicitly (R2)
    res = c.post("/v1/research/cycle", json={"watchlist_id": wid}).json()
    assert res["signals"] and all("R2" in s["failed_checks"] for s in res["signals"])

    assert (
        c.post("/v1/risk/kill-switch", json={"action": "release", "reason": ""}).status_code == 422
    )
    off = c.post("/v1/risk/kill-switch", json={"action": "release", "reason": "drill over"})
    assert off.json()["kill_switch_active"] is False and off.json()["changed"] is True
    hist = c.get("/v1/risk/controls").json()["history"]
    assert [h["action"] for h in hist[:2]] == ["RELEASE", "ACTIVATE"]
    with pytest.raises(DBAPIError, match="append-only"), session_scope(_sf(env4)) as s:
        s.execute(text("DELETE FROM control_events"))


def test_strategy_pause_and_resume(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    assert c.post("/v1/strategies/nope/pause", json={"reason": "x" * 5}).status_code == 404
    p = c.post(f"/v1/strategies/{STRAT}/pause", json={"reason": "review"}).json()
    assert p["changed"] is True
    r = _cycle(env4, aid, wid).json()
    assert r["new_orders"] == 0 and r["signals"][0]["failed_checks"] == ["R2"]
    assert STRAT in c.get("/v1/risk/controls").json()["paused_strategies"]
    c.post(f"/v1/strategies/{STRAT}/resume", json={"reason": "reviewed"})
    assert c.get("/v1/risk/controls").json()["paused_strategies"] == {}


def test_daily_loss_stop_auto_pauses_the_strategy(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    with session_scope(_sf(env4)) as s:
        acct = s.get(PaperAccount, aid)
        assert acct is not None
        day = SIGNAL_AT.astimezone(EXCHANGE_TZ).date().isoformat()
        # Earlier cycles today: the intraday ledger held positions worth 100k at the start of
        # the day and they are gone with no cash back -> a 100k ledger loss, past the 1.5 % stop.
        # (Phase 3/4 flat state format: the cycle must still read it as the INTRADAY ledger.)
        acct.state = {
            "day": day,
            "last_bar": {},
            "entries": {},
            "pnl_day": day,
            "day_flow": "0",
            "day_start_value": "100000",
            "day_start_equity": "1000000",
        }
    r = _cycle(env4, aid, wid).json()
    assert r["new_orders"] == 0 and r["auto_paused"] == [STRAT]
    paused = c.get("/v1/risk/controls").json()["paused_strategies"][STRAT]
    assert paused["actor_type"] == "system" and "R8" in paused["reason"]
    # the system cannot resume it; a user can, with a reason
    assert c.post(f"/v1/strategies/{STRAT}/resume", json={"reason": "reviewed"}).json()["changed"]


# ---------------------------------------------------------------------------- reconciliation
def test_reconciliation_break_trips_kill_switch(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    _cycle(env4, aid, wid)
    ok = c.post(f"/v1/paper/accounts/{aid}/reconcile").json()
    assert ok["status"] == "OK" and ok["trigger"] == "manual"
    with session_scope(_sf(env4)) as s:
        s.execute(text("UPDATE paper_accounts SET cash = cash - 1"))
    brk = c.post(f"/v1/paper/accounts/{aid}/reconcile").json()
    assert brk["status"] == "BREAK" and brk["diffs"][0]["kind"] == "CASH_MISMATCH"
    ctl = c.get("/v1/risk/controls").json()
    assert ctl["kill_switch_active"] is True
    assert ctl["kill_switch"]["actor_type"] == "system"
    triggers = [r["trigger"] for r in c.get(f"/v1/reconciliations?account_id={aid}").json()]
    assert triggers[:2] == ["manual", "manual"] and "pre-cycle" in triggers
    # next cycle reconciles first, still breaks, and nothing new is opened
    env4.clock.advance(timedelta(minutes=5))
    r = _cycle(env4, aid, wid).json()
    assert all(s["order_id"] is None for s in r["signals"] if s["type"] == "ENTER_LONG")


def test_consolidated_view_sums_ledgers(env4: Env) -> None:
    c = env4.c
    _, aid = _setup(env4)
    with session_scope(_sf(env4)) as s:
        for h, q in (("INTRADAY", 10), ("LONG_TERM", 40)):
            s.execute(
                text(
                    "INSERT INTO paper_positions (account_id, horizon, exchange, symbol, quantity,"
                    " average_price, realized_pnl, charges_paid) VALUES"
                    " (:a, :h, 'NSE', :sym, :q, 100, 0, 0)"
                ),
                {"a": aid, "h": h, "sym": KEY.symbol, "q": q},
            )
    (row,) = c.get(f"/v1/portfolio/consolidated?account_id={aid}").json()["positions"]
    assert row["instrument"] == str(KEY) and row["quantity"] == 50
    assert set(row["by_ledger"]) == {"INTRADAY", "LONG_TERM"}
    ledgers = {
        lg["horizon"]: lg
        for lg in c.get(f"/v1/portfolio/ledgers?account_id={aid}").json()["ledgers"]
    }
    assert ledgers["LONG_TERM"]["open_positions"] == 1


# ---------------------------------------------------------------------------- UNKNOWN orders
def test_submit_timeout_leaves_unknown_then_resolves_without_resending(env4: Env) -> None:
    c = env4.c
    wid, aid = _setup(env4)
    st = c.app.state.rhythm  # type: ignore[attr-defined]
    venue = FlakyVenue(mode="timeout_after_accept", fail_next=1)

    def run() -> paper.CycleResult:
        with session_scope(st.session_factory) as s:
            wl = wl_svc.get_watchlist(s, wid)
            keys = [InstrumentKey(Exchange(i.exchange), i.symbol) for i in wl.items]
            return paper.run_cycle(
                s,
                keys=keys,
                provider=st.market_data,
                strategy=IntradayOrbVwap(),
                settings=st.settings,
                clock=st.clock,
                calendar=st.calendar,
                audit=st.audit,
                actor_id="test",
                account_id=aid,
                venue=venue,
            )

    run()
    (o,) = c.get(f"/v1/paper/accounts/{aid}/orders").json()
    assert o["state"] == "UNKNOWN" and o["display"] == "NEEDS_ATTENTION"
    assert len(venue.day_orders()) == 1

    env4.clock.advance(timedelta(seconds=30))
    res = run()
    assert res.unknown_resolution == {o["id"]: "ACKNOWLEDGED"}
    (o2,) = [x for x in c.get(f"/v1/paper/accounts/{aid}/orders").json() if x["id"] == o["id"]]
    assert o2["state"] == "BROKER_ACKNOWLEDGED" and o2["venue_order_id"] == "FLAKY-00000001"
    assert len(venue.day_orders()) == 1  # never re-sent
    events = [e["to"] for e in c.get(f"/v1/paper/orders/{o['id']}/events").json()]
    assert events[-3:] == ["SUBMITTED", "UNKNOWN", "BROKER_ACKNOWLEDGED"]
    with session_scope(_sf(env4)) as s:
        n = s.execute(select(func.count()).select_from(RiskCheckRow)).scalar_one()
        assert n >= 1
        assert s.execute(select(func.count()).select_from(ControlEventRow)).scalar_one() == 0
    assert json.loads(json.dumps(c.get("/v1/audit/verify").json()))["ok"] is True
