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

export const api = {
  get: <T>(path: string) => call<T>(path),
  post: <T>(path: string, body?: unknown) =>
    call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
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
