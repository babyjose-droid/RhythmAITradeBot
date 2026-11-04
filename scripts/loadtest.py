#!/usr/bin/env python3
"""Concurrent load test against a running API (Phase 9). Read-only GET requests only.

    python3 scripts/loadtest.py --api http://127.0.0.1:8000 --workers 8 --seconds 30
    # with sign-in on, pass a session cookie from the browser:  --cookie rhythm_session=...

Fails (exit 1) if p95 > --p95-ms (default 300, PRODUCT_REQUIREMENTS §8) or any request
errors. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request

PATHS = [
    "/v1/watchlists",
    "/v1/paper/accounts",
    "/v1/signals?limit=100",
    "/v1/risk/controls",
    "/v1/approvals",
    "/v1/notifications/summary",
    "/v1/automation",
    "/v1/audit/events?limit=100",
    "/v1/scheduler/status",
    "/health/ready",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--p95-ms", type=float, default=300)
    ap.add_argument("--cookie", default="")
    a = ap.parse_args()
    if not a.api.startswith(("http://", "https://")):
        raise SystemExit("--api must be http(s)")
    paths = list(PATHS)
    try:
        with urllib.request.urlopen(a.api + "/v1/paper/accounts", timeout=10) as r:  # noqa: S310
            accts = json.loads(r.read())
        if accts:
            aid = accts[0]["id"]
            paths += [
                f"/v1/paper/accounts/{aid}",
                f"/v1/paper/accounts/{aid}/orders",
                f"/v1/portfolio/consolidated?account_id={aid}",
            ]
    except (urllib.error.URLError, ValueError, KeyError):
        pass
    lat: dict[str, list[float]] = {p: [] for p in paths}
    errors: list[str] = []
    lock = threading.Lock()
    stop = time.monotonic() + a.seconds

    def worker(n: int) -> None:
        i = n
        while time.monotonic() < stop:
            p = paths[i % len(paths)]
            i += 1
            hdrs = {"Cookie": a.cookie} if a.cookie else {}
            req = urllib.request.Request(a.api + p, headers=hdrs)  # noqa: S310 (http(s) checked)
            t = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                    r.read()
                    ok = r.status < 500 and (r.status == 200 or p == "/health/ready")
            except urllib.error.HTTPError as e:
                ok = p == "/health/ready" and e.code == 503  # not-ready is a valid answer
            except urllib.error.URLError as e:
                ok = False
                with lock:
                    errors.append(f"{p}: {e.reason}")
            dt = time.perf_counter() - t
            with lock:
                lat[p].append(dt)
                if not ok and len(errors) < 50:
                    errors.append(f"{p}: failed")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    allv = [v for vs in lat.values() for v in vs]
    q = statistics.quantiles(allv, n=100)
    print(
        f"{len(allv)} requests in {a.seconds:.0f}s with {a.workers} workers "
        f"({len(allv) / a.seconds:.0f} req/s); errors: {len(errors)}"
    )
    print(
        f"overall  p50 {q[49] * 1e3:6.1f} ms   p95 {q[94] * 1e3:6.1f} ms"
        f"   p99 {q[98] * 1e3:6.1f} ms"
    )
    for p, vs in sorted(
        lat.items(), key=lambda kv: -statistics.quantiles(kv[1], n=20)[18] if len(kv[1]) > 1 else 0
    ):
        if len(vs) > 1:
            print(f"  p95 {statistics.quantiles(vs, n=20)[18] * 1e3:6.1f} ms  n={len(vs):5d}  {p}")
    for e in errors[:5]:
        print("  error:", e)
    return 0 if q[94] * 1e3 <= a.p95_ms and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
