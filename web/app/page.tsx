"use client";

import { ErrorBox, money, useData } from "@/components/ui";
import { api, type Automation, type NotificationSummary, type PaperAccount, type Readiness } from "@/lib/api";

export default function Overview() {
  const ready = useData(() => api.get<Readiness>("/health/ready").catch(() => null));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const auto = useData(() => api.get<Automation>("/v1/automation"));
  const notes = useData(() => api.get<NotificationSummary>("/v1/notifications/summary"));

  return (
    <>
      <h1>Portfolio dashboard</h1>
      <section className="card">
        <h2>Needs attention</h2>
        <table>
          <tbody>
            <tr>
              <td>Kill switch</td>
              <td>
                {auto.data == null ? (
                  "—"
                ) : (
                  <span className={`tone tone-${auto.data.kill_switch_active ? "fail" : "ok"}`}>
                    {auto.data.kill_switch_active ? "ACTIVE — new entries blocked" : "off"}
                  </span>
                )}
              </td>
            </tr>
            <tr>
              <td>Approvals waiting</td>
              <td>
                <a href="/approvals">{auto.data?.approvals.pending ?? "—"}</a>
              </td>
            </tr>
            <tr>
              <td>Unread notifications</td>
              <td>
                <a href="/notifications">
                  {notes.data?.unread_total ?? "—"}
                  {notes.data?.unread.critical ? ` (${notes.data.unread.critical} critical)` : ""}
                </a>
              </td>
            </tr>
            <tr>
              <td>Paused strategies</td>
              <td>{auto.data?.strategies.filter((s) => s.paused).map((s) => s.name).join(", ") || "none"}</td>
            </tr>
          </tbody>
        </table>
      </section>
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
