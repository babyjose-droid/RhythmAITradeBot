/** Minimal typed client for the Rhythm API (proxied at /api by next.config.mjs). */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Cookie set by the API at sign-in; echoed in X-CSRF-Token on every mutating request. */
export function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const m = document.cookie.match(/(?:^|;\s*)rhythm_csrf=([^;]+)/);
  return m?.[1] ? decodeURIComponent(m[1]) : null;
}

/**
 * Sensitive actions (risk limits, kill-switch release, user admin) need a recent
 * re-authentication. The layout registers a dialog that asks for password + code; the
 * request is then retried once. Nothing is retried without the user's explicit re-auth.
 */
let reauthHandler: (() => Promise<boolean>) | null = null;
export function setReauthHandler(fn: (() => Promise<boolean>) | null): void {
  reauthHandler = fn;
}

async function call<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const csrf = method === "GET" ? null : csrfToken();
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
    credentials: "same-origin",
  });
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body ? String((body as { detail: unknown }).detail) : res.statusText;
    if (res.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
    if (res.status === 403 && detail === "reauth_required" && !retried && reauthHandler) {
      if (await reauthHandler()) return call<T>(path, init, true);
    }
    throw new ApiError(res.status, detail === "reauth_required" ? "Re-authentication required for this action" : detail);
  }
  return body as T;
}

/** Window event fired after a control change so always-visible banners refresh at once. */
export const CONTROLS_CHANGED = "rhythm:controls-changed";

/** Every mutating request carries a fresh Idempotency-Key, so a retried click is not re-run. */
function idempotencyKey(): string {
  const c = globalThis.crypto;
  return `ui-${c?.randomUUID ? c.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
}

function send<T>(method: "POST" | "PUT", path: string, body?: unknown): Promise<T> {
  return call<T>(path, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: { "Idempotency-Key": idempotencyKey() },
  });
}

export const api = {
  get: <T>(path: string) => call<T>(path),
  post: <T>(path: string, body?: unknown) => send<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => send<T>("PUT", path, body),
};

export interface Mode {
  environment: string;
  trading_mode: string;
  orders_permitted: boolean;
  live_trading_available: boolean;
  features: Record<string, boolean>;
  server_time_exchange: string;
}

export interface Readiness {
  status: string;
  checks: { name: string; ok: boolean; detail: Record<string, unknown> }[];
}

export interface Watchlist {
  id: number;
  name: string;
  horizon: string;
  items: { instrument: string; active_in_registry: boolean }[];
}

export interface Freshness {
  instrument: string;
  horizon: string;
  source: string;
  is_mock: boolean;
  freshness: { status: string; usable_for_new_orders: boolean; age_seconds: number | null; reasons: string[] };
}

export interface PaperAccount {
  id: number;
  name: string;
  account_kind: "PAPER";
  cash: string;
  starting_cash?: string;
}

export interface PaperAccountDetail extends PaperAccount {
  realized_pnl: string;
  positions: {
    instrument: string;
    horizon: string;
    quantity: number;
    average_price: string;
    stop: string | null;
    target: string | null;
    opened_at: string | null;
  }[];
}

export interface Order {
  id: string;
  account_kind: "PAPER";
  instrument: string;
  side: string;
  order_type: string;
  purpose: string;
  quantity: number;
  filled_quantity: number;
  average_fill_price: string | null;
  state: string;
  display: string;
  data_is_mock: boolean;
  created_at: string;
}

export interface Fill {
  id: string;
  order_id: string;
  instrument: string;
  side: string;
  quantity: number;
  price: string;
  charges_total: string;
  at: string;
}

export interface SignalRow {
  id: number;
  kind: "CANDIDATE";
  context: string;
  instrument: string;
  strategy: string;
  signal_type: string;
  decision_state: string;
  decided_at: string;
  ref_price: string;
  stop: string | null;
  target: string | null;
  order_id: string | null;
  assessment_id?: number | null;
  horizon?: string;
  data_is_mock: boolean;
}

export interface BacktestRun {
  id: number;
  strategy: string;
  version: string;
  symbols: string[];
  start: string;
  end: string;
  data_is_mock: boolean;
  run_hash: string;
  metrics: Record<string, unknown>;
  splits: Record<string, { metrics: Record<string, unknown> }>;
  cost_model: { version: string; verified: boolean };
}

export interface ControlEvent {
  id: number;
  at: string;
  action: "ACTIVATE" | "RELEASE";
  actor_type: string;
  actor_id: string;
  reason: string;
  control?: string;
  target?: string;
}

export interface Controls {
  kill_switch_active: boolean;
  kill_switch: ControlEvent | null;
  paused_strategies: Record<string, ControlEvent>;
  history: ControlEvent[];
}

export interface CheckResult {
  code: string;
  name: string;
  status: string;
  detail: string;
  max_qty: number | null;
}

export interface RiskCheck {
  id: number;
  context: string;
  account_id: number | null;
  horizon: string;
  strategy: string;
  instrument: string;
  side: string;
  intent: string;
  purpose: string;
  decided_at: string;
  outcome: string;
  quantity: number;
  policy_version: string;
  order_id: string | null;
  checks: CheckResult[];
}

export interface Policies {
  engine_version: string;
  active: Record<string, unknown> & { version: string };
  note: string;
  history: { version: string; effective_from: string; approved_by: string; reason: string }[];
}

export interface LedgerSummary {
  horizon: string;
  allocated_capital: string;
  exposure: string;
  utilisation_pct: number | null;
  open_positions: number;
  realized_pnl: string;
  charges_paid: string;
}

export interface Reconciliation {
  id: number;
  run_at: string;
  trigger: string;
  status: "OK" | "BREAK";
  diffs: { kind: string; subject: string; expected: string; actual: string }[];
  summary: Record<string, unknown>;
}

export interface Consolidated {
  account_id: number;
  positions: {
    instrument: string;
    quantity: number;
    by_ledger: Record<string, { quantity: number; average_price: string }>;
    mark: string | null;
    mark_source: string;
    value: string;
  }[];
  reconciliation: Reconciliation | null;
}

export interface CalendarEvent {
  instrument: string;
  kind: string;
  event_date: string;
  days_away: number;
  known_at: string;
  source: string;
  source_ref: string;
  is_mock: boolean;
}

export interface EventListing {
  events: CalendarEvent[];
  last_sync: Record<string, string | null>;
}

export interface Thesis {
  id: number;
  instrument: string;
  strategy: string;
  status: "PENDING_ENTRY" | "OPEN" | "INVALIDATED" | "CLOSED";
  rationale: string[];
  invalidation: Record<string, unknown>[];
  ref_price: string;
  stop: string | null;
  target: string | null;
  entry_order_id: string;
  opened_at: string;
  last_checked_at: string | null;
  last_check: Record<string, unknown>;
  next_review_at: string | null;
  closed_at: string | null;
  close_reason: string | null;
  data_is_mock: boolean;
  events?: { at: string; from: string | null; to: string; reason: string; detail: Record<string, unknown> }[];
}

export interface CycleResult {
  mode: string;
  horizon: string;
  diagnostics: Record<string, Record<string, unknown>>;
  new_orders: number;
  new_fills: number;
  signals: { instrument: string; type: string; decision: string; failed_checks: string[]; reasons: string[] }[];
  skipped: Record<string, string>;
  theses: Record<string, number>;
}

export interface DerivedMetric {
  value: string | null;
  formula: string;
  inputs: string[];
}

export interface Fundamentals {
  instrument: string;
  as_of: string;
  price: string | null;
  price_source: string;
  is_mock: boolean;
  quarters: { period_end: string; known_at: string; source: string; source_ref: string; metrics: Record<string, string> }[];
  derived: Record<string, DerivedMetric>;
}

// ------------------------------------------------------------------ research (Phase 7)
export interface ResearchStatus {
  enabled: boolean;
  provider: string;
  feature_flag: boolean;
  gate_paper_entries: boolean;
  prompt: { id: string; sha256: string };
  allowed_actions: string[];
  authority: string;
  model?: string;
  is_mock?: boolean;
  max_assessments_per_cycle?: number;
  budget?: {
    day: string;
    tokens_used: number;
    token_budget: number;
    tokens_remaining: number;
    assessments: number;
    cost_usd: string | null;
    cost_rates_configured: boolean;
    resets_at: string;
  };
}

export interface AssessmentOutput {
  observed_facts: { statement: string; source_ids: string[] }[];
  bull_case: string[];
  bear_case: string[];
  catalysts: string[];
  invalidation_conditions: string[];
  key_metrics_with_provenance: { name: string; value: string; source_ids: string[] }[];
  suggested_action: string;
  rationale: string;
  confidence_explanation: string;
  key_uncertainties: string[];
  change_view_conditions: string[];
  evidence_sufficient: boolean;
}

export interface Assessment {
  id: number;
  created_at: string;
  context: string;
  signal_id: number | null;
  instrument: string;
  horizon: string;
  signal_type: string;
  provider: string;
  model: string;
  is_mock: boolean;
  prompt_version: string;
  status: string;
  suggested_action: string | null;
  gate: { allow: boolean; decision: string; reason: string };
  errors: string[];
  usage: {
    input_tokens: number;
    output_tokens: number;
    estimated: boolean;
    cost_usd: string | null;
    latency_ms: number;
    attempts: number;
  };
  output: AssessmentOutput | null;
  raw_excerpt?: string;
  packet?: {
    id: number;
    sha256: string;
    content: {
      facts: { id: string; name: string; value: string; unit: string; source: string; source_ts: string | null }[];
      derived_metrics: { id: string; name: string; value: string | null; formula: string; inputs: string[] }[];
      missing: string[];
      assumptions: string[];
      untrusted_documents: { id: string; source: string; text: string }[];
    };
  } | null;
}

// ------------------------------------------------------------------ Phase 8
export interface Me {
  username: string;
  role: "owner" | "approver" | "viewer";
  auth_enabled: boolean;
  mfa_required?: boolean;
  recently_reauthenticated: boolean;
}

export interface AuthStatus {
  enabled: boolean;
  mfa_required?: boolean;
  has_users: boolean;
  setup_hint?: string | null;
}

export interface Approval {
  id: number;
  created_at: string;
  account_id: number;
  signal_id: number | null;
  instrument: string;
  horizon: string;
  signal_type: string;
  strategy: string;
  source: "RISK" | "RESEARCH";
  reason: string;
  detail: {
    decided_at: string;
    ref_price: string;
    stop: string | null;
    target: string | null;
    target_weight: string | null;
    reasons: string[];
    thesis: Record<string, unknown> | null;
    risk: { outcome: string; quantity: number; policy_version: string };
    assessment_id: number | null;
  };
  data_is_mock: boolean;
  expires_at: string;
  status: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "FAILED";
  decided_at: string | null;
  decided_by: string | null;
  decision_reason: string | null;
  decision_risk: { outcome: string; quantity: number; quote_status: string; price: string } | null;
  order_id: string | null;
}

export interface Notification {
  id: number;
  created_at: string;
  kind: string;
  severity: "info" | "warning" | "critical";
  title: string;
  body: string;
  link: string | null;
  read_at: string | null;
  read_by: string | null;
  webhook: { status: string; attempts: number; error: string | null };
}

export interface NotificationSummary {
  unread: Record<"info" | "warning" | "critical", number>;
  unread_total: number;
  webhook_configured: boolean;
  webhook_min_severity: string;
}

export interface AuditEventRow {
  id: number;
  occurred_at: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  subject_type: string | null;
  subject_id: string | null;
  payload: Record<string, unknown>;
  row_hash: string;
}

export interface Automation {
  trading_mode: string;
  kill_switch_active: boolean;
  strategies: {
    name: string;
    version: string;
    horizon: string;
    paused: boolean;
    run_after: string;
    last_run: {
      at: string;
      by: string;
      kind: string;
      account_id: string | null;
      new_orders: number | null;
      new_fills: number | null;
      signals: number | null;
      data_is_mock: boolean | null;
    } | null;
  }[];
  approvals: { policy: string; pending: number };
  research: { provider: string; gate_paper_entries: boolean };
  notifications: { webhook_configured: boolean; min_severity: string };
  scheduler: {
    configured: boolean;
    note: string;
    expected?: boolean;
    enabled_jobs?: number;
    jobs?: number;
    leader_heartbeat_age_seconds?: number | null;
  };
}

export interface UserRowOut {
  username: string;
  role: string;
  created_at: string;
  created_by: string;
  disabled: boolean;
  locked_until: string | null;
  failed_logins: number;
}

export interface Enrollment {
  username: string;
  role: string;
  totp_secret: string;
  provisioning_uri: string;
  note: string;
}

// ------------------------------------------------------------------ Phase 9
export interface ScheduledJob {
  id: number;
  name: string;
  kind: "paper_cycle" | "events_sync" | "fundamentals_sync" | "health_check" | "audit_verify";
  params: Record<string, unknown>;
  schedule: "intraday_5m" | "after_close" | "daily_at" | "every_minutes";
  schedule_arg: string | null;
  trading_days_only: boolean;
  enabled: boolean;
  created_at: string;
  created_by: string;
  next_run_at: string | null;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  consecutive_failures: number;
  disabled_reason: string | null;
}

export interface JobRun {
  id: number;
  job_id: number;
  slot: string;
  trigger: string;
  started_at: string;
  finished_at: string | null;
  status: "RUNNING" | "SUCCESS" | "FAILED" | "SKIPPED" | "MISSED";
  duration_ms: number | null;
  instance: string;
  detail: Record<string, unknown>;
}

export interface SchedulerStatus {
  expected: boolean;
  tick_seconds: number;
  leader_heartbeat_age_seconds: number | null;
  stale_after_seconds: number;
  instances: { instance: string; is_leader: boolean; started_at: string; last_beat_at: string; age_seconds: number }[];
  health: { component: string; status: "ok" | "degraded" | "down"; detail: Record<string, unknown> }[];
}
