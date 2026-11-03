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

## Swing routine (PAPER, Phase 5)

After the close (from about 15:45 IST): sync the event calendar (at least weekly; R18 requires ≤ 7 days), run the PAPER swing cycle, then review the Swing workspace:
- theses marked INVALIDATED have an exit queued for the next open;
- watch the "never — entries blocked" coverage rows.

The next morning's cycle is not needed: fills happen when the next after-close cycle processes that session.

## Long-term routine (PAPER, Phase 6)

After the close: sync fundamentals (daily is fine — nothing is rewritten) and the event calendar (weekly), run the long-term cycle, then review the Long-term page: new-results reviews, any REDUCE/ADD rebalances, and INVALIDATED theses (exit queued for the next open). Remember every fundamental number is MOCK.

## Research service (Phase 7, advisory)

- **Status and budget:** `GET /v1/research/status`, or the AI research page. The budget resets at IST midnight. When it is exhausted, assessments are stored as BUDGET_EXCEEDED without a call.
- **If the PAPER entry gate is on:** any non-CONFIRM result holds the entry. Typical causes are timeouts, invalid replies or an exhausted budget. Held candidates appear with decision HOLD / NO_ACTION / REQUIRES_REVIEW, and their assessment is linked from the candidate row. To proceed without research, turn the gate off and restart. Exits are never affected.
- **Invalid replies:** read the errors and the raw excerpt on the assessment page. Never copy numbers from a rejected reply.
- **Key handling:** if a key is ever exposed, revoke it at the provider and replace it in the secret store. Never put it in `.env` files that are committed, or in chat.

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
