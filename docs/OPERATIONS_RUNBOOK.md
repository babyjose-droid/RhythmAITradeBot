# Operations Runbook

**Status:** Phase 0 skeleton plus Phase 4 control procedures (below). Full procedures are written and rehearsed in Phases 8–9.

## Daily (trading days, IST)

1. Before 09:00 — owner completes Sharekhan login (token valid 24 h, no refresh documented); system verifies session, clock sync, data feeds, reconciliation; mode shown on dashboard.
2. 09:15–15:30 — monitor freshness, risk utilisation, alerts.
3. After close — EOD reconciliation, scrip-master snapshot archive, backups, daily report (PAPER labelled).

## Incidents (to be detailed)

Data feed stale · broker 403/token expired · 429 storm · RMS down (502) · reconciliation break · UNKNOWN orders · kill-switch activation & release · DB failover/restore · suspected secret leak (rotate API key/Secret Key with Sharekhan, revoke tokens).

## Health

`/health/live`, `/health/ready`, scheduler heartbeat, `system_health_events`.

## Phase 4 control procedures (PAPER)

- **Kill switch:**
  - Activate: dashboard → Risk & controls → type a reason → *Activate kill switch*, or `POST /v1/risk/kill-switch {"action":"activate","reason":...}` with an `Idempotency-Key`.
  - Effect: open entry orders are cancelled, and stops/targets remain. Tick *flatten* to also square off intraday positions on the next cycle.
  - Release: same screen, with a new reason. Only a person can release.
- **Reconciliation break:**
  - The kill switch activates automatically (actor `system:reconciliation`).
  - Open Portfolio & reconciliation and read the diffs (cash / position / order-fill / orphan fill).
  - Find the cause before releasing. The fill journal is append-only and is the reference.
- **Daily loss stop:** the strategy is auto-paused (`system:risk-engine`). Review the day's decisions in Risk & controls → Recent risk decisions, then resume with a reason, or leave it paused.
- **UNKNOWN order** ("UNKNOWN — check"):
  - Never re-send. The next cycle matches it against the venue order book.
  - If it stays ambiguous, compare the venue's day orders by hand.
  - New orders on that instrument stay blocked (R16) until the order is resolved.
- **Risk policy change:** `POST /v1/risk/policies {"params": {...}, "reason": ...}` creates a new version, effective immediately. Values outside the hard bounds are rejected.
