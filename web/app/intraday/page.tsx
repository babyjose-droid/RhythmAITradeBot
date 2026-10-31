"use client";

import { useState } from "react";

import { DecisionBadge, ErrorBox, FreshnessPill, MockTag, OrderBadge, money, useData, when } from "@/components/ui";
import {
  api,
  type Fill,
  type Freshness,
  type Order,
  type PaperAccount,
  type PaperAccountDetail,
  type SignalRow,
  type Watchlist,
} from "@/lib/api";

export default function IntradayWorkspace() {
  const watchlists = useData(() => api.get<Watchlist[]>("/v1/watchlists"));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const [wid, setWid] = useState<number | null>(null);
  const [aid, setAid] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const wlId = wid ?? watchlists.data?.find((w) => w.horizon === "INTRADAY" || w.horizon === "ANY")?.id ?? null;
  const acctId = aid ?? accounts.data?.[0]?.id ?? null;

  const fresh = useData(
    () => (wlId == null ? Promise.resolve([]) : api.get<Freshness[]>(`/v1/marketdata/freshness?watchlist_id=${wlId}&horizon=INTRADAY`)),
    [wlId],
  );
  const detail = useData(
    () => (acctId == null ? Promise.resolve(null) : api.get<PaperAccountDetail>(`/v1/paper/accounts/${acctId}`)),
    [acctId],
  );
  const orders = useData(
    () => (acctId == null ? Promise.resolve([]) : api.get<Order[]>(`/v1/paper/accounts/${acctId}/orders?limit=50`)),
    [acctId],
  );
  const fills = useData(
    () => (acctId == null ? Promise.resolve([]) : api.get<Fill[]>(`/v1/paper/accounts/${acctId}/fills?limit=50`)),
    [acctId],
  );
  const signals = useData(
    () => api.get<SignalRow[]>(acctId == null ? "/v1/signals?limit=50" : `/v1/signals?account_id=${acctId}&limit=50`),
    [acctId],
  );

  const refreshAll = async () => {
    await Promise.all([fresh.refresh(), detail.refresh(), orders.refresh(), fills.refresh(), signals.refresh()]);
  };

  async function run(kind: "paper" | "research") {
    if (wlId == null) return;
    setBusy(true);
    setErr(null);
    try {
      const path = kind === "paper" && acctId != null ? `/v1/paper/accounts/${acctId}/cycle` : "/v1/research/cycle";
      const r = await api.post<{ mode: string; new_orders: number; new_fills: number; signals: unknown[] }>(path, {
        watchlist_id: wlId,
      });
      setMsg(`${r.mode} cycle: ${r.signals.length} candidate(s), ${r.new_orders} new PAPER order(s), ${r.new_fills} simulated fill(s)`);
      await refreshAll();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function createAccount() {
    setErr(null);
    try {
      const a = await api.post<PaperAccount>("/v1/paper/accounts", { name: `paper-${Date.now()}`, starting_cash: "1000000" });
      await accounts.refresh();
      setAid(a.id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <>
      <h1>Intraday workspace</h1>
      <ErrorBox error={err ?? watchlists.error ?? accounts.error} />
      {msg && <div className="note">{msg}</div>}

      <section className="card controls">
        <label>
          Watchlist{" "}
          <select value={wlId ?? ""} onChange={(e) => setWid(Number(e.target.value))}>
            {watchlists.data?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name} ({w.horizon})
              </option>
            ))}
          </select>
        </label>
        <label>
          PAPER account{" "}
          <select value={acctId ?? ""} onChange={(e) => setAid(Number(e.target.value))}>
            {accounts.data?.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <button onClick={createAccount}>New PAPER account</button>
        <button disabled={busy || wlId == null} onClick={() => run("research")}>
          Run research cycle (no orders)
        </button>
        <button className="primary" disabled={busy || wlId == null || acctId == null} onClick={() => run("paper")}>
          Run PAPER cycle
        </button>
        <button onClick={refreshAll}>Refresh</button>
      </section>

      <section className="card">
        <h2>Data status</h2>
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Freshness</th>
              <th>Age (s)</th>
              <th>Source</th>
              <th>Reasons</th>
            </tr>
          </thead>
          <tbody>
            {fresh.data?.map((f) => (
              <tr key={f.instrument}>
                <td>{f.instrument}</td>
                <td>
                  <FreshnessPill status={f.freshness.status} />
                </td>
                <td>{f.freshness.age_seconds?.toFixed(1) ?? "—"}</td>
                <td>
                  {f.source} <MockTag show={f.is_mock} />
                </td>
                <td className="muted">{f.freshness.reasons.join("; ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Candidates (ideas — not orders)</h2>
        <table>
          <thead>
            <tr>
              <th>Decided</th>
              <th>Instrument</th>
              <th>Status</th>
              <th>Ref price</th>
              <th>Stop / target</th>
              <th>Linked PAPER order</th>
            </tr>
          </thead>
          <tbody>
            {signals.data?.map((s) => (
              <tr key={s.id}>
                <td>{when(s.decided_at)}</td>
                <td>
                  {s.instrument} <MockTag show={s.data_is_mock} />
                </td>
                <td>
                  <DecisionBadge decision={s.decision_state} /> <span className="muted">{s.context}</span>
                </td>
                <td>{money(s.ref_price)}</td>
                <td>
                  {money(s.stop)} / {money(s.target)}
                </td>
                <td className="mono">{s.order_id ?? "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          PAPER orders <span className="tag-paper">PAPER</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Created</th>
              <th>Instrument</th>
              <th>Side / type / purpose</th>
              <th>Status</th>
              <th>Qty (filled)</th>
              <th>Avg fill</th>
            </tr>
          </thead>
          <tbody>
            {orders.data?.map((o) => (
              <tr key={o.id}>
                <td>{when(o.created_at)}</td>
                <td>
                  {o.instrument} <MockTag show={o.data_is_mock} />
                </td>
                <td>
                  {o.side} {o.order_type} · {o.purpose}
                </td>
                <td>
                  <OrderBadge display={o.display} /> <span className="muted">{o.state}</span>
                </td>
                <td>
                  {o.quantity} ({o.filled_quantity})
                </td>
                <td>{money(o.average_fill_price)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          Simulated fills <span className="tag-paper">PAPER</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Instrument</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Charges</th>
              <th>Order</th>
            </tr>
          </thead>
          <tbody>
            {fills.data?.map((f) => (
              <tr key={f.id} className="row-fill">
                <td>{when(f.at)}</td>
                <td>{f.instrument}</td>
                <td>{f.side}</td>
                <td>{f.quantity}</td>
                <td>{money(f.price)}</td>
                <td>{money(f.charges_total)}</td>
                <td className="mono">{f.order_id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          PAPER positions <span className="tag-paper">PAPER</span>
        </h2>
        {detail.data && (
          <p>
            Cash {money(detail.data.cash)} · realised P&amp;L {money(detail.data.realized_pnl)}
          </p>
        )}
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Qty</th>
              <th>Avg price</th>
              <th>Stop</th>
              <th>Target</th>
            </tr>
          </thead>
          <tbody>
            {detail.data?.positions.map((p) => (
              <tr key={p.instrument}>
                <td>{p.instrument}</td>
                <td>{p.quantity}</td>
                <td>{money(p.average_price)}</td>
                <td>{money(p.stop)}</td>
                <td>{money(p.target)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
