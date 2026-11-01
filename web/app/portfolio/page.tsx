"use client";

import { useState } from "react";

import { ErrorBox, Tone, money, useData, when } from "@/components/ui";
import { api, CONTROLS_CHANGED, type Consolidated, type LedgerSummary, type PaperAccount, type Reconciliation } from "@/lib/api";

export default function Portfolio() {
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const [aid, setAid] = useState<number | null>(null);
  const acctId = aid ?? accounts.data?.[0]?.id ?? null;
  const ledgers = useData(
    () =>
      acctId == null
        ? Promise.resolve(null)
        : api.get<{ cash: string; ledgers: LedgerSummary[] }>(`/v1/portfolio/ledgers?account_id=${acctId}`),
    [acctId],
  );
  const cons = useData(
    () => (acctId == null ? Promise.resolve(null) : api.get<Consolidated>(`/v1/portfolio/consolidated?account_id=${acctId}`)),
    [acctId],
  );
  const recons = useData(
    () => (acctId == null ? Promise.resolve([]) : api.get<Reconciliation[]>(`/v1/reconciliations?account_id=${acctId}&limit=20`)),
    [acctId],
  );
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function reconcile() {
    if (acctId == null) return;
    setBusy(true);
    setErr(null);
    try {
      await api.post(`/v1/paper/accounts/${acctId}/reconcile`);
      window.dispatchEvent(new Event(CONTROLS_CHANGED)); // a break trips the kill switch
      await Promise.all([cons.refresh(), recons.refresh()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const latest = cons.data?.reconciliation;

  return (
    <>
      <h1>Portfolio &amp; reconciliation</h1>
      <ErrorBox error={err ?? accounts.error ?? ledgers.error ?? cons.error} />
      <section className="card controls">
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
        <button disabled={busy || acctId == null} onClick={reconcile}>
          Reconcile now
        </button>
        {latest && (
          <span>
            Last reconciliation <Tone value={latest.status === "OK" ? "PASS" : "FAIL"} /> {latest.status} at{" "}
            {when(latest.run_at)} ({latest.trigger})
          </span>
        )}
      </section>

      <section className="card paper-frame">
        <h2>
          Ledgers by horizon <span className="tag-paper">PAPER</span>
        </h2>
        <p className="muted">Shared account cash {money(ledgers.data?.cash)}. Allocations are illustrative paper settings.</p>
        <table>
          <thead>
            <tr>
              <th>Ledger</th>
              <th>Allocated</th>
              <th>Exposure</th>
              <th>Utilisation</th>
              <th>Open positions</th>
              <th>Realised P&amp;L</th>
              <th>Charges</th>
            </tr>
          </thead>
          <tbody>
            {ledgers.data?.ledgers.map((l) => (
              <tr key={l.horizon}>
                <td>{l.horizon}</td>
                <td>{money(l.allocated_capital)}</td>
                <td>{money(l.exposure)}</td>
                <td>{l.utilisation_pct == null ? "—" : `${l.utilisation_pct.toFixed(1)} %`}</td>
                <td>{l.open_positions}</td>
                <td>{money(l.realized_pnl)}</td>
                <td>{money(l.charges_paid)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          Consolidated positions (all ledgers) <span className="tag-paper">PAPER</span>
        </h2>
        {cons.data?.positions.length === 0 && <p className="muted">No open PAPER positions.</p>}
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Total qty</th>
              <th>By ledger</th>
              <th>Mark</th>
              <th>Value</th>
            </tr>
          </thead>
          <tbody>
            {cons.data?.positions.map((p) => (
              <tr key={p.instrument}>
                <td>{p.instrument}</td>
                <td>{p.quantity}</td>
                <td>
                  {Object.entries(p.by_ledger)
                    .map(([h, v]) => `${h} ${v.quantity}`)
                    .join(" · ")}
                </td>
                <td>
                  {p.mark ? money(p.mark) : "—"} <span className="muted">{p.mark_source}</span>
                </td>
                <td>{money(p.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Reconciliation history</h2>
        <p className="muted">
          Checks positions, cash and order fill quantities against the append-only PAPER fill journal. A break
          activates the kill switch.
        </p>
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Trigger</th>
              <th>Status</th>
              <th>Differences</th>
            </tr>
          </thead>
          <tbody>
            {recons.data?.map((r) => (
              <tr key={r.id}>
                <td>{when(r.run_at)}</td>
                <td>{r.trigger}</td>
                <td>
                  <Tone value={r.status === "OK" ? "PASS" : "FAIL"} /> {r.status}
                </td>
                <td className="muted">
                  {r.diffs.length === 0
                    ? "none"
                    : r.diffs.map((d) => `${d.kind} ${d.subject}: expected ${d.expected}, got ${d.actual}`).join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
