# Deployment Guide

**Status:** Local stack defined in Phase 1; image build verified in GitHub Actions. Hosted deployment in Phase 9.

## Local (Phase 1 target)

`docker compose up --build` → `db` (Postgres 16, healthchecked) → `migrate` (`alembic upgrade head`, runs once) → `api` (non-root container, healthcheck on `/health/live`, published on `127.0.0.1:8000`). Then `web` (Next.js dashboard, non-root, published on `127.0.0.1:3000`, proxies `/api/*` to the `api` service). Configuration via `.env` copied from `.env.example` (placeholders only). Default `TRADING_MODE=RESEARCH_ONLY`.

## Container images (automatic)

Every push to `main` that passes all CI checks publishes the API image to GitHub Container Registry using the workflow's built-in token (no stored credentials):

- `ghcr.io/babyjose-droid/rhythmaitradebot:main` — latest passing `main`
- `ghcr.io/babyjose-droid/rhythmaitradebot:sha-<7-char-commit>` — immutable, per commit
- `ghcr.io/babyjose-droid/rhythmaitradebot-web:main` and `:sha-<commit>` — dashboard image (Phase 3)

Optional Docker Hub mirror (`rhythmconsultancy/rhythm-ai-trader`): the CI step runs only if the repository secret `DOCKERHUB_TOKEN` exists (owner adds it in GitHub → Settings → Secrets; never in code).

Pull and run (with a PostgreSQL URL of your own):
```bash
docker pull ghcr.io/babyjose-droid/rhythmaitradebot:main
```
New packages on GHCR are private by default; change visibility under the package's *Package settings* if needed. For AWS (Phase 9) the same image will be mirrored to ECR.

## Hosted (later)

AWS-compatible: container service, managed Postgres (encryption at rest, PITR), secrets manager, private subnets, TLS termination, central logs/metrics/alerts, NTP-synchronised hosts. **A fixed egress IP may be needed** if static-IP whitelisting applies to API orders (Blocker B-01) — decide before choosing hosting.

## Path to AWS (planned, after all phases)

GitHub (CI green) → container registry (e.g. ECR) → ECS Fargate or similar, RDS PostgreSQL, Secrets Manager, private subnets, ALB with TLS + authentication in front of the API (Phase 8 auth is a prerequisite), CloudWatch logs/alarms, NAT with a fixed egress IP if static-IP whitelisting applies (B-01). Final testing round runs there with PAPER mode only.
