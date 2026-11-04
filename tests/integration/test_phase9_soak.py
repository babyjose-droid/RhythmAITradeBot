"""Phase 9 soak: several simulated trading days driven only by the scheduler.

Run with ``pytest -m soak`` (``SOAK_DAYS``, default 5). Asserts every slot ran exactly once
and succeeded, intraday books are flat after each close, reconciliation stayed clean, the
audit chain is intact, and memory did not grow without bound.
"""

from __future__ import annotations

import os
import statistics
import time
import tracemalloc
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select

from rhythm.db.models import JobRunRow, OrderRow, PaperPosition, ReconciliationRow
from rhythm.db.session import session_scope
from rhythm.scheduler.service import Scheduler
from tests.integration.test_phase6_api import CAL, START
from tests.integration.test_phase9_api import Env9, at

pytestmark = [pytest.mark.integration, pytest.mark.soak]


def test_multi_day_soak(db_url: str, migrated: Engine) -> None:
    days = int(os.environ.get("SOAK_DAYS", "5"))
    e = Env9(db_url, migrated)
    with e.c as c:
        wid, aid = e.seed()
        iwl = c.post("/v1/watchlists", json={"name": "intra", "horizon": "INTRADAY"}).json()["id"]
        for sym in ("NSE:MOCKA", "NSE:MOCKB"):
            c.post(f"/v1/watchlists/{iwl}/items", json={"instrument": sym})
        swl = c.post("/v1/watchlists", json={"name": "swing", "horizon": "SWING"}).json()["id"]
        for sym in ("NSE:MOCKC", "NSE:MOCKD"):
            c.post(f"/v1/watchlists/{swl}/items", json={"instrument": sym})

        def job(**b: Any) -> int:
            jid = int(e.job(**b)["id"])
            e.enable(jid)
            return jid

        ids = {
            "intraday": job(
                name="intraday",
                kind="paper_cycle",
                schedule="intraday_5m",
                params={"account_id": aid, "watchlist_id": iwl, "strategy": "intraday_orb_vwap"},
            ),
            "swing": job(
                name="swing",
                kind="paper_cycle",
                schedule="after_close",
                params={"account_id": aid, "watchlist_id": swl, "strategy": "swing_trend_pullback"},
            ),
            "long": job(
                name="long",
                kind="paper_cycle",
                schedule="after_close",
                params={
                    "account_id": aid,
                    "watchlist_id": wid,
                    "strategy": "long_term_quality_value",
                },
            ),
            "events": job(
                name="events",
                kind="events_sync",
                schedule="daily_at",
                schedule_arg="08:30",
                params={"watchlist_id": swl},
            ),
            "health": job(
                name="health", kind="health_check", schedule="every_minutes", schedule_arg="15"
            ),
            "audit": job(
                name="audit", kind="audit_verify", schedule="daily_at", schedule_arg="18:00"
            ),
        }
        sched = Scheduler(e.state, instance="soak")
        tracemalloc.start()
        # Continuous time from the moment the jobs were enabled (15:40 on START) to the end
        # of the Nth following trading day, every 5 minutes; non-trading days are skipped
        # (no slot falls on them), so nothing should ever be MISSED.
        t, d, trading, ticks = at(START, 15, 40), START, 0, []
        base_mem = 0
        while True:
            d = t.astimezone(at(START, 0).tzinfo).date()
            if not CAL.is_trading_day(d):
                t = at(d + timedelta(days=1), 0, 0)
                continue
            e.clock.set(t + timedelta(seconds=20))
            start = time.perf_counter()
            sched.tick()
            ticks.append(time.perf_counter() - start)
            t += timedelta(minutes=5)
            if t.astimezone(at(START, 0).tzinfo).date() != d:  # midnight: a trading day ended
                with session_scope(e.state.session_factory) as s:
                    open_intraday = s.execute(
                        select(func.count())
                        .select_from(PaperPosition)
                        .where(PaperPosition.horizon == "INTRADAY", PaperPosition.quantity != 0)
                    ).scalar_one()
                assert open_intraday == 0, f"intraday positions left open after {d}"
                if d == START:
                    base_mem = tracemalloc.get_traced_memory()[0]
                    continue
                trading += 1
                if trading == days:
                    break
        mem = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        sched.release()

        with session_scope(e.state.session_factory) as s:
            rows = s.execute(
                select(JobRunRow.job_id, JobRunRow.status, func.count()).group_by(
                    JobRunRow.job_id, JobRunRow.status
                )
            ).all()
        by = {(j, st): n for j, st, n in rows}
        bad = {k: v for k, v in by.items() if k[1] != "SUCCESS"}
        assert bad == {}, bad
        assert by[(ids["intraday"], "SUCCESS")] == 75 * days
        assert by[(ids["events"], "SUCCESS")] == days
        for k in ("swing", "long", "audit"):  # also ran on START, when they were enabled
            assert by[(ids[k], "SUCCESS")] == days + 1, k
        assert by[(ids["health"], "SUCCESS")] >= 96 * days
        with session_scope(e.state.session_factory) as s:
            recon = dict(
                s.execute(
                    select(ReconciliationRow.status, func.count()).group_by(
                        ReconciliationRow.status
                    )
                ).all()
            )
            n_orders = s.execute(select(func.count()).select_from(OrderRow)).scalar_one()
        assert recon.get("OK", 0) > 0 and recon.get("BREAK", 0) == 0, recon
        assert c.get("/v1/audit/verify").json()["ok"] is True
        growth_mb = (mem - base_mem) / 1e6
        p50 = statistics.median(ticks) * 1e3
        p95 = statistics.quantiles(ticks, n=20)[18] * 1e3
        print(
            f"\nsoak: {days} trading days, {len(ticks)} ticks, tick p50 {p50:.0f} ms, "
            f"p95 {p95:.0f} ms, max {max(ticks) * 1e3:.0f} ms; {n_orders} PAPER orders; "
            f"{recon['OK']} reconciliations (all OK); traced memory growth {growth_mb:.1f} MB"
        )
        assert growth_mb < 100
