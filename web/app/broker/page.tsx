"use client";

import { useEffect, useState } from "react";

import { ErrorBox, money, useData, when } from "@/components/ui";
import { api } from "@/lib/api";

interface Check {
  check: string;
  endpoint: string;
  outcome: "OK" | "EMPTY" | "ERROR";
  http_status: number | null;
  latency_ms: number | null;
  detail: string | null;
}

interface Status {
  adapter: string;
  configured: boolean;
  read_only: boolean;
  order_placement: string;
  market_open: boolean;
  connected?: boolean;
  account_ref?: string | null;
  exchanges?: string[];
  connected_at?: string | null;
  expires_at?: string | null;
  expires_in_minutes?: number | null;
  last_verification?: { run_id: string; observed_at: string; checks: Check[] } | null;
}

interface Holding {
  symbol: string;
  exchange: string;
  quantity: number;
  average_price: string | null;
}
interface Position {
  symbol: string;
  exchange: string;
  product: string;
  net_quantity: number;
  average_price: string | null;
}
interface Order {
  broker_order_id: string;
  symbol: string;
  side: string;
  quantity: number;
  filled_quantity: number;
  status: string;
}
interface Funds {
  cash_balance: string;
  as_of: string;
  note: string;
}
interface Stream {
  connected: boolean;
  messages: number;
  first_message_ms: number | null;
  error: string | null;
  feed_keys: string[];
  market_open: boolean;
  note: string | null;
}

const TONE: Record<string, string> = { OK: "ok", EMPTY: "reduce", ERROR: "fail" };

export default function Broker() {
  const st = useData(() => api.get<Status>("/v1/broker/status"));
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [account, setAccount] = useState<{ funds: Funds; holdings: Holding[]; positions: Position[]; orders: Order[] } | null>(null);
  const [stream, setStream] = useState<Stream | null>(null);

  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("connected")) setMsg("Connected to Sharekhan (read only). The session lasts up to 24 hours.");
    if (q.get("error")) setErr(`Sharekhan sign-in did not complete: ${q.get("error")}`);
  }, []);

  const d = st.data;
  const connected = Boolean(d?.connected);

  async function run<T>(fn: () => Promise<T>, after?: (v: T) => void) {
    setBusy(true);
    setErr(null);
    try {
      const v = await fn();
      after?.(v);
      await st.refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const connect = () =>
    run(
      () => api.post<{ login_url: string }>("/v1/broker/login"),
      (v) => window.location.assign(v.login_url),
    );

  const loadAccount = () =>
    run(async () => {
      const [funds, holdings, positions, orders] = await Promise.all([
        api.get<Funds>("/v1/broker/funds"),
        api.get<Holding[]>("/v1/broker/holdings"),
        api.get<Position[]>("/v1/broker/positions"),
        api.get<Order[]>("/v1/broker/orders"),
      ]);
      setAccount({ funds, holdings, positions, orders });
    });

  return (
    <>
      <h1>Sharekhan account (read only)</h1>
      <section className="card notif-critical">
        <strong>READ ONLY.</strong> This connection reads your profile, funds, holdings, positions, today&apos;s orders,
        the instrument list and price history. The software contains no code that can place, change or cancel an
        order. You sign in on Sharekhan&apos;s own page; Rhythm never sees your Sharekhan password.
      </section>
      <ErrorBox error={st.error ?? err} />
      {msg && <p className="tone tone-ok">{msg}</p>}

      {d && !d.configured && (
        <section className="card">
          <h2>Not configured</h2>
          <p>
            Add your API Key and Secure Key to <span className="mono">.env</span> on your computer (see README,
            &quot;Connect Sharekhan&quot;), set <span className="mono">RHYTHM_BROKER_ADAPTER=sharekhan</span>, and restart.
            Never paste the keys into a chat or a web page.
          </p>
        </section>
      )}

      {d?.configured && (
        <section className="card">
          <h2>Connection</h2>
          {connected ? (
            <p>
              Connected as account <span className="mono">{d.account_ref}</span> ({(d.exchanges ?? []).join(", ")}) since{" "}
              {when(d.connected_at)}; expires {when(d.expires_at)} (in {d.expires_in_minutes} min).
            </p>
          ) : (
            <p>Not connected. Sessions last at most 24 hours and cannot be refreshed, so you sign in once a day.</p>
          )}
          <p className="muted">Market {d.market_open ? "open" : "closed"} now (NSE 09:15–15:30 IST).</p>
          <div className="row">
            <button disabled={busy} onClick={connect}>
              {connected ? "Reconnect" : "Connect Sharekhan"}
            </button>
            {connected && (
              <>
                <button disabled={busy} onClick={() => run(() => api.post("/v1/broker/verify", {}))}>
                  Run read-only verification
                </button>
                <button
                  disabled={busy}
                  onClick={() => run(() => api.post<Stream>("/v1/broker/verify/stream", { seconds: 10 }), setStream)}
                >
                  Test price stream (10 s)
                </button>
                <button disabled={busy} onClick={loadAccount}>
                  Show account
                </button>
                <button disabled={busy} onClick={() => run(() => api.post("/v1/broker/logout"), () => setAccount(null))}>
                  Disconnect
                </button>
              </>
            )}
          </div>
        </section>
      )}

      {d?.last_verification && (
        <section className="card">
          <h2>Last verification ({when(d.last_verification.observed_at)})</h2>
          <p className="muted">Only field names, types, counts and timings are stored, never your account values.</p>
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Endpoint</th>
                <th>Result</th>
                <th>HTTP</th>
                <th>ms</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {d.last_verification.checks.map((c) => (
                <tr key={c.check}>
                  <td>{c.check}</td>
                  <td className="mono">{c.endpoint}</td>
                  <td>
                    <span className={`tone tone-${TONE[c.outcome] ?? "reduce"}`}>{c.outcome}</span>
                  </td>
                  <td>{c.http_status ?? "–"}</td>
                  <td>{c.latency_ms ?? "–"}</td>
                  <td>{c.detail ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {stream && (
        <section className="card">
          <h2>Price stream check</h2>
          <p>
            {stream.connected ? "Connected" : "Not connected"} · {stream.messages} message(s)
            {stream.first_message_ms !== null && ` · first after ${stream.first_message_ms} ms`}
            {stream.error && ` · error: ${stream.error}`} · {stream.feed_keys.join(", ")}
          </p>
          {stream.note && <p className="muted">{stream.note}</p>}
        </section>
      )}

      {account && (
        <>
          <section className="card">
            <h2>Funds (NSE)</h2>
            <p>
              Cash balance reported by Sharekhan: <strong>{money(account.funds.cash_balance)}</strong> · as of{" "}
              {when(account.funds.as_of)}
            </p>
            <p className="muted">{account.funds.note}</p>
          </section>
          <section className="card">
            <h2>Holdings ({account.holdings.length})</h2>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Qty</th>
                  <th>Avg price</th>
                </tr>
              </thead>
              <tbody>
                {account.holdings.map((h) => (
                  <tr key={`${h.exchange}:${h.symbol}`}>
                    <td>{h.symbol}</td>
                    <td>{h.quantity}</td>
                    <td>{money(h.average_price)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="card">
            <h2>Positions ({account.positions.length})</h2>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Product</th>
                  <th>Net qty</th>
                  <th>Avg price</th>
                </tr>
              </thead>
              <tbody>
                {account.positions.map((p) => (
                  <tr key={`${p.exchange}:${p.symbol}:${p.product}`}>
                    <td>{p.symbol}</td>
                    <td>{p.product}</td>
                    <td>{p.net_quantity}</td>
                    <td>{money(p.average_price)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <section className="card">
            <h2>Today&apos;s orders ({account.orders.length})</h2>
            <p className="muted">As reported by Sharekhan. Rhythm cannot change these.</p>
            <table>
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Symbol</th>
                  <th>Side</th>
                  <th>Qty</th>
                  <th>Filled</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {account.orders.map((o) => (
                  <tr key={o.broker_order_id}>
                    <td className="mono">{o.broker_order_id}</td>
                    <td>{o.symbol}</td>
                    <td>{o.side}</td>
                    <td>{o.quantity}</td>
                    <td>{o.filled_quantity}</td>
                    <td>{o.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </>
  );
}
