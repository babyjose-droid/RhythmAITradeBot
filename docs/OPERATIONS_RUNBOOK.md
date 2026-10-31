# Operations Runbook

**Status:** Phase 0 skeleton — procedures are written and rehearsed in Phases 8–9.

## Daily (trading days, IST)

1. Before 09:00 — owner completes Sharekhan login (token valid 24 h, no refresh documented); system verifies session, clock sync, data feeds, reconciliation; mode shown on dashboard.
2. 09:15–15:30 — monitor freshness, risk utilisation, alerts.
3. After close — EOD reconciliation, scrip-master snapshot archive, backups, daily report (PAPER labelled).

## Incidents (to be detailed)

Data feed stale · broker 403/token expired · 429 storm · RMS down (502) · reconciliation break · UNKNOWN orders · kill-switch activation & release · DB failover/restore · suspected secret leak (rotate API key/Secret Key with Sharekhan, revoke tokens).

## Health

`/health/live`, `/health/ready`, scheduler heartbeat, `system_health_events`.
