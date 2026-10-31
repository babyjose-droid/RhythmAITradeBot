# System Architecture

**Status:** Phase 0 proposal — awaiting product-owner approval. Nothing here is implemented yet.

## 1. Starting point

The repository is greenfield: no existing code, dependencies, tests or conventions were found (see README "Phase 0 findings"). We therefore adopt the blueprint's proposed stack.

| Concern | Choice | Reason |
|---|---|---|
| Backend | Python 3.12+, FastAPI, Pydantic v2 | Typed schemas, analytics ecosystem |
| Persistence | PostgreSQL 16, SQLAlchemy 2.x, Alembic | Durable, transactional ledgers; migrations |
| Cache / coordination | Redis — **deferred** until a concrete need (rate-limit tokens, WebSocket fan-out) | Avoid unnecessary infra |
| Scheduling | APScheduler in-process (single scheduler leader), IST-aware market calendar | Modular monolith; no queue needed initially |
| Analytics | pandas, NumPy; Decimal for money | Float-free accounting |
| HTTP / WS clients | httpx, websockets (TLS verified) | See SHAREKHAN_INTEGRATION.md §10 |
| AI | Anthropic Claude API behind `ResearchProvider` | Advisory only |
| Frontend | Next.js + TypeScript (from Phase 3, expanded Phase 8) | Blueprint |
| Quality | pytest, hypothesis, ruff, mypy (strict on core), pre-commit, GitHub Actions | |
| Runtime | Docker Compose locally (api, db, web); AWS-compatible later | |

## 2. Modular monolith — module boundaries

```
rhythm/
  config/          settings, feature flags, TradingMode (RESEARCH_ONLY | PAPER | LIVE[locked])
  core/            clock (UTC + IST), market calendar, money/qty types, ids, errors
  audit/           append-only audit_events, hash-chained
  instruments/     registry, scrip-master snapshots, watchlists, universes (point-in-time)
  marketdata/      MarketDataProvider protocol, ingestion, freshness monitor, data_quality_events
  broker/          BrokerAdapter protocol; mock, paper, sharekhan (read-only first)
  strategies/      Strategy protocol; intraday/, swing/, longterm/ (versioned, pure functions)
  research/        evidence packets, ResearchProvider (Claude), schema validation, prompt versions
  portfolio/       virtual ledgers per horizon, consolidated broker view, orchestrator
  risk/            RiskEngine — deterministic, no imports from research/
  orders/          OrderManager, order state machine, idempotency, reconciliation
  paper/           simulated execution, fees/slippage model, paper accounts
  backtest/        event-driven engine reusing strategies/, risk/, paper/ fill model
  notifications/   alerts (email/Slack/webhook later)
  api/             FastAPI routers (read models + authorised commands)
  web/             Next.js app (separate package)
```

Dependency rules (enforced by an import-linter contract in CI):
- `risk/` must not import `research/`. LLM output reaches risk only as a validated, typed `Candidate`.
- `research/` cannot import `orders/` or `broker/` — it has no path to submit anything.
- Only `orders/` may call `BrokerAdapter` write methods; those methods are unavailable unless the LIVE gate is open (never before Phase 10).
- `strategies/` are pure: input = point-in-time data snapshot, output = signals. No I/O.

## 3. Core interfaces (signatures, illustrative)

```python
class MarketDataProvider(Protocol):
    def latest_quote(self, inst: InstrumentId) -> Quote            # carries source, source_ts, received_ts
    def bars(self, inst, interval, start, end) -> BarSeries        # provenance per bar
    def subscribe(self, insts) -> AsyncIterator[Quote]

class Strategy(Protocol):
    horizon: Horizon; version: str
    def evaluate(self, ctx: StrategyContext) -> list[Signal]       # deterministic

class ResearchProvider(Protocol):
    async def assess(self, packet: EvidencePacket, prompt: PromptVersion) -> AssessmentResult
        # returns validated AIAssessment or a NO_ACTION/REQUIRES_REVIEW fallback; never raises to caller

class RiskEngine(Protocol):
    def evaluate(self, proposal: OrderProposal, state: RiskState) -> RiskDecision  # ACCEPT | REDUCE | BLOCK | REVIEW

class OrderManager(Protocol):
    def propose(self, decision: PortfolioDecision) -> Order        # state PROPOSED
    def advance(self, order_id, event: OrderEvent) -> Order        # legal transitions only

class BrokerAdapter(Protocol): ...  # see SHAREKHAN_INTEGRATION.md §11
```

## 4. Decision pipeline

```
 market data ─┐
 broker state ┼─► Strategy (per horizon, own schedule) ─► Signal
 fundamentals ┘                                            │
                                     EvidencePacket ◄──────┤
                                            │              │
                              Claude (advisory) ─► AIAssessment (schema-validated)
                                                           │
                       Strategy rule reconciliation ◄──────┘  (LLM can only downgrade/hold, never invent a trade the rules didn't produce)
                                            │
                          Portfolio orchestrator (consolidated exposure, funds, open orders, cross-horizon netting)
                                            │  PortfolioDecision: BUY_/ADD_/REDUCE_/SELL_/EXIT_CANDIDATE | HOLD | NO_ACTION
                                            ▼
                              Independent RiskEngine ─► ACCEPT / REDUCE / BLOCK(REJECTED_BY_RISK) / REVIEW(REQUIRES_REVIEW)
                                            │
                              Approval policy (human, if configured)
                                            │
                          Mode switch: RESEARCH_ONLY → stop (display only)
                                       PAPER         → paper execution simulator
                                       LIVE          → locked (Phase 10 gate)
                                            │
                              Reconciliation ─► audit + dashboard
```

**Key principle on LLM authority:** a candidate action must originate from deterministic strategy rules. Claude's assessment may confirm, downgrade to HOLD/NO_ACTION, or flag REQUIRES_REVIEW. It may never upgrade size, change limits, or create an order on its own. (Product-owner decision D-07 asks whether Claude may *originate* long-term ideas for human review only.)

## 5. Ledgers: separate strategy books, one broker truth

- Each horizon has a **virtual ledger** (`virtual_ledgers`, `positions` with `ledger_id`): allocated capital, positions, thesis, P&L attribution.
- The **consolidated view** sums ledgers per instrument and compares against broker holdings/positions/funds (`broker_reconciliations`). Any unexplained difference ⇒ `RECONCILIATION_BREAK` ⇒ risk engine blocks new orders on that instrument (and globally if funds are unreconciled).
- Cross-horizon example: intraday sells 50 of a stock the long-term book holds 200 of. The orchestrator tags the sell to the intraday ledger, and for cash equity must ensure the intraday sell is matched by an intraday buy (not drawn from long-term delivery holdings) — product-type mapping is Blocker B-02.
- Paper and live are **physically separate**: paper rows live under `paper_accounts` with `account_kind = PAPER`; a DB check constraint and separate API routes prevent mixing. Every paper screen and report carries a PAPER banner.

## 6. Order lifecycle (state machine)

```
PROPOSED → RISK_CHECKED → (APPROVAL_PENDING → APPROVED | APPROVAL_REJECTED)
         → SUBMITTED → BROKER_ACKNOWLEDGED → PARTIALLY_FILLED → FILLED
                                         ↘ CANCELLED | BROKER_REJECTED | EXPIRED
         → RISK_REJECTED
SUBMITTED + timeout → UNKNOWN (must be resolved by reconciliation; blocks resubmission)
any terminal → RECONCILED
```
Every transition is an `order_events` row (UTC time, actor, source, raw payload ref). Illegal transitions raise. UI uses distinct shapes, colours and labels for *Candidate*, *Approved order*, *Broker-accepted order* and *Fill* (see PRODUCT_REQUIREMENTS.md §6).

## 7. Time and data provenance

- Store UTC (`timestamptz`); retain `exchange_tz = 'Asia/Kolkata'` and the source's original timestamp string.
- Every market datum: `source`, `source_ts`, `received_ts`, `ingest_run_id`, `quality_flags`. Freshness thresholds per horizon (e.g. intraday quote stale > N seconds — configurable).
- Mock/sample data is tagged `source = 'MOCK'` and can never be written to production tables in a non-test environment.

## 8. Failure posture — fail closed

Stale data, unknown broker state, risk service exception, missing required fields, clock skew > threshold, token absent/expired, reconciliation break, kill switch → no new orders; existing paper orders follow the kill-switch policy (RISK_MANAGEMENT.md §7).

## 9. Deployment shape (later)

Single API container + scheduler (leader-elected), Postgres (managed, encrypted, PITR backups), web container, secrets manager, private network for DB, structured logs to a central sink, health/metrics endpoints. Details in DEPLOYMENT_GUIDE.md.
