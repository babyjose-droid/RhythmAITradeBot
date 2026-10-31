"use client";

import { ErrorBox, money, useData } from "@/components/ui";
import { api, type PaperAccount, type Readiness } from "@/lib/api";

export default function Overview() {
  const ready = useData(() => api.get<Readiness>("/health/ready").catch(() => null));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));

  return (
    <>
      <h1>Overview</h1>
      <section className="card">
        <h2>System readiness</h2>
        {ready.data == null ? (
          <p className="pill pill-stale">⚠ NOT READY or unreachable — the system fails closed</p>
        ) : (
          <table>
            <tbody>
              {ready.data.checks.map((c) => (
                <tr key={c.name}>
                  <td>{c.name}</td>
                  <td>
                    <span className={`pill ${c.ok ? "pill-ok" : "pill-stale"}`}>{c.ok ? "OK" : "FAIL"}</span>
                  </td>
                  <td className="muted">{JSON.stringify(c.detail)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card paper-frame">
        <h2>
          PAPER accounts <span className="tag-paper">PAPER</span>
        </h2>
        <ErrorBox error={accounts.error} />
        {accounts.data?.length === 0 && <p className="muted">No PAPER accounts yet. Create one in the intraday workspace.</p>}
        <table>
          <thead>
            <tr>
              <th>Account</th>
              <th>Cash</th>
              <th>Starting cash</th>
            </tr>
          </thead>
          <tbody>
            {accounts.data?.map((a) => (
              <tr key={a.id}>
                <td>
                  {a.name} <span className="tag-paper">PAPER</span>
                </td>
                <td>{money(a.cash)}</td>
                <td>{money(a.starting_cash)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
