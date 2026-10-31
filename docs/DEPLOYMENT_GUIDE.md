# Deployment Guide

**Status:** Local stack defined in Phase 1 (image build not yet verified — see PHASE_1_REPORT.md); hosted deployment in Phase 9.

## Local (Phase 1 target)

`docker compose up --build` → `db` (Postgres 16, healthchecked) → `migrate` (`alembic upgrade head`, runs once) → `api` (non-root container, healthcheck on `/health/live`, published on `127.0.0.1:8000`). Later: `web` (Next.js). Configuration via `.env` copied from `.env.example` (placeholders only). Default `TRADING_MODE=RESEARCH_ONLY`.

## Hosted (later)

AWS-compatible: container service, managed Postgres (encryption at rest, PITR), secrets manager, private subnets, TLS termination, central logs/metrics/alerts, NTP-synchronised hosts. **A fixed egress IP may be needed** if static-IP whitelisting applies to API orders (Blocker B-01) — decide before choosing hosting.

## Path to AWS (planned, after all phases)

GitHub (CI green) → container registry (e.g. ECR) → ECS Fargate or similar, RDS PostgreSQL, Secrets Manager, private subnets, ALB with TLS + authentication in front of the API (Phase 8 auth is a prerequisite), CloudWatch logs/alarms, NAT with a fixed egress IP if static-IP whitelisting applies (B-01). Final testing round runs there with PAPER mode only.
