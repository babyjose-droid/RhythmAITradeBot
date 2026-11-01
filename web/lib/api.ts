/** Minimal typed client for the Rhythm API (proxied at /api by next.config.mjs). */

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body ? String((body as { detail: unknown }).detail) : res.statusText;
    throw new ApiError(res.status, detail);
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
  positions: { instrument: string; quantity: number; average_price: string; stop: string | null; target: string | null }[];
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
