#!/usr/bin/env python3
"""Fill a LOCAL stack with MOCK demo data and run PAPER cycles (no broker, no real orders).

Prerequisite: the API runs in PAPER mode with mock sources (see demo.env):

    docker compose --env-file .env --env-file demo.env up -d

Usage:  python3 scripts/demo_paper.py [--api http://127.0.0.1:8000]
Safe to run repeatedly: existing watchlists/accounts are reused. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

SYMBOLS = ["NSE:MOCKA", "NSE:MOCKB", "NSE:MOCKC"]


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"content-type": "application/json"}
        if method in ("POST", "PUT"):
            headers["Idempotency-Key"] = f"demo-{uuid.uuid4().hex}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode(errors="replace")


def step(msg: str) -> None:
    print(f"\n== {msg}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    api = Api(ap.parse_args().api)

    code, mode = api.call("GET", "/v1/system/mode")
    if code != 200:
        print(f"API not reachable at {api.base} ({code}). Is `docker compose up -d` running?")
        return 1
    print(f"mode={mode['trading_mode']} env={mode['environment']}")
    code, ready = api.call("GET", "/health/ready")
    if code != 200:
        print(f"API is up but not ready ({code}): {ready}")
        print("Check `docker compose ps` and `docker compose logs api`.")
        return 1
    if mode["trading_mode"] != "PAPER" or not mode["features"].get("paper_trading"):
        print("Not in PAPER mode. Start the stack with the demo overrides:")
        print("  docker compose --env-file .env --env-file demo.env up -d")
        return 2

    step("Instruments (MOCK scrip master)")
    print(api.call("POST", "/v1/instruments/sync")[1])

    _, existing = api.call("GET", "/v1/watchlists")
    by_name = {w["name"]: w for w in existing}
    wl: dict[str, int] = {}
    for name, horizon in (("Demo intraday", "INTRADAY"), ("Demo swing", "SWING")):
        if name in by_name:
            wl[horizon] = by_name[name]["id"]
        else:
            wl[horizon] = api.call("POST", "/v1/watchlists", {"name": name, "horizon": horizon})[1][
                "id"
            ]
        for s in SYMBOLS:
            api.call("POST", f"/v1/watchlists/{wl[horizon]}/items", {"instrument": s})
    print(f"watchlists: {wl}")

    step("PAPER account")
    _, accounts = api.call("GET", "/v1/paper/accounts")
    acct = next((a for a in accounts if a["name"] == "Demo PAPER"), None)
    if acct is None:
        acct = api.call(
            "POST", "/v1/paper/accounts", {"name": "Demo PAPER", "starting_cash": "1000000"}
        )[1]
    aid = acct["id"]
    print(f"account id {aid} (PAPER, starting cash 10,00,000)")

    step("Event calendar (MOCK results dates) for the swing gate R18")
    print(api.call("POST", "/v1/events/sync", {"watchlist_id": wl["SWING"]})[1])

    step("Intraday PAPER cycle (does nothing useful outside 09:15-15:30 IST on weekdays)")
    code, r = api.call("POST", f"/v1/paper/accounts/{aid}/cycle", {"watchlist_id": wl["INTRADAY"]})
    print(
        code,
        {k: r.get(k) for k in ("new_orders", "new_fills", "skipped")} if isinstance(r, dict) else r,
    )

    step("Swing PAPER cycle (decides on the latest completed daily session)")
    code, r = api.call(
        "POST",
        f"/v1/paper/accounts/{aid}/cycle",
        {"watchlist_id": wl["SWING"], "strategy": "swing_trend_pullback"},
    )
    if isinstance(r, dict):
        print(code, {k: r.get(k) for k in ("new_orders", "new_fills", "theses", "skipped")})
        for s in r.get("signals", []):
            print("  ", s["instrument"], s["type"], s["decision"], s.get("failed_checks"))
    else:
        print(code, r)

    step("Backtests on MOCK data (pipeline checks, not evidence of edge)")
    now = datetime.now(timezone.utc)
    for strategy, days in (("intraday_orb_vwap", 20), ("swing_trend_pullback", 400)):
        code, r = api.call(
            "POST",
            "/v1/backtests",
            {
                "strategy": strategy,
                "symbols": SYMBOLS[:2],
                "start": (now - timedelta(days=days)).isoformat(),
                "end": now.isoformat(),
                "data_source": "provider",
            },
        )
        trades = r.get("trades") if isinstance(r, dict) else r
        print(f"{strategy}: HTTP {code}, trades={trades}")

    print("\nDone. Open the dashboard: http://127.0.0.1:3300")
    print("Everything shown is MOCK data in PAPER mode — no broker, no real orders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
