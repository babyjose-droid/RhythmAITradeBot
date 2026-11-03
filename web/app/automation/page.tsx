"use client";

import { useState } from "react";

import { useMe } from "@/components/Session";
import { ErrorBox, MockTag, useData, when } from "@/components/ui";
import { api, type Automation, type PaperAccount, type Watchlist } from "@/lib/api";
import { canAct } from "@/lib/status";

export default function AutomationCentre() {
  const me = useMe();
  const ov = useData(() => api.get<Automation>("/v1/automation"));
  const watchlists = useData(() => api.get<Watchlist[]>("/v1/watchlists"));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const [aid, setAid] = useState<number | null>(null);
  const [wl, setWl] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const acct = aid ?? accounts.data?.[0]?.id ?? null;
  const owner = canAct(me?.role, "own");

  function defaultWl(horizon: string): number | undefined {
    return watchlists.data?.find((w) => w.horizon === horizon)?.id ?? watchlists.data?.find((w) => w.horizon === "ANY")?.id;
  }

  async function run(name: string, horizon: string) {
    const wid = wl[name] ?? defaultWl(horizon);
    if (acct == null || wid == null) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ new_orders: number; new_fills: number; signals: unknown[] }>(
        `/v1/paper/accounts/${acct}/cycle`,
        { watchlist_id: wid, strategy: name },
      );
      setMsg(`${name}: ${r.signals.length} candidate(s), ${r.new_orders} new PAPER order(s), ${r.new_fills} fill(s)`);
      await ov.refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const d = ov.data;
  return (
    <>
      <h1>Automation centre</h1>
      <p className="muted">
        What runs, when, and under which guards. Every run goes through the same strategy → risk engine → (approval)
        → PAPER path. Live trading is locked.
      </p>
      <ErrorBox error={err ?? ov.error} />
      {msg && <div className="note">{msg}</div>}
      {d && (
        <section className="card">
          <h2>Guards</h2>
          <table>
            <tbody>
              <tr>
                <td>Mode</td>
                <td>{d.trading_mode}</td>
              </tr>
              <tr>
                <td>Kill switch</td>
                <td>
                  <span className={`tone tone-${d.kill_switch_active ? "fail" : "ok"}`}>
                    {d.kill_switch_active ? "ACTIVE — entries blocked" : "off"}
                  </span>
                </td>
              </tr>
              <tr>
                <td>Approvals</td>
                <td>
                  {d.approvals.policy} · <a href="/approvals">{d.approvals.pending} pending</a>
                </td>
              </tr>
              <tr>
                <td>AI research gate</td>
                <td>
                  {d.research.provider === "none"
                    ? "research service off"
                    : `${d.research.provider} · PAPER entry gate ${d.research.gate_paper_entries ? "ON" : "off"}`}
                </td>
              </tr>
              <tr>
                <td>Notifications</td>
                <td>
                  in-app{d.notifications.webhook_configured ? ` + webhook (≥ ${d.notifications.min_severity})` : " only"}
                </td>
              </tr>
              <tr>
                <td>Scheduler</td>
                <td className="muted">{d.scheduler.note}</td>
              </tr>
            </tbody>
          </table>
        </section>
      )}
      <section className="card paper-frame">
        <h2>
          Strategies <span className="tag-paper">PAPER</span>
        </h2>
        <label>
          PAPER account{" "}
          <select value={acct ?? ""} onChange={(e) => setAid(Number(e.target.value))}>
            {accounts.data?.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        </label>
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Horizon</th>
              <th>Status</th>
              <th>Runs</th>
              <th>Last run</th>
              <th>Watchlist</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {d?.strategies.map((s) => (
              <tr key={s.name}>
                <td>
                  {s.name}@{s.version}
                </td>
                <td>{s.horizon}</td>
                <td>
                  <span className={`tone tone-${s.paused ? "fail" : "ok"}`}>{s.paused ? "PAUSED" : "active"}</span>
                </td>
                <td className="muted">{s.run_after}</td>
                <td>
                  {s.last_run ? (
                    <>
                      {when(s.last_run.at)} by {s.last_run.by}
                      <div className="muted">
                        account {s.last_run.account_id} · {s.last_run.new_orders ?? 0} order(s), {s.last_run.new_fills ?? 0}{" "}
                        fill(s) <MockTag show={!!s.last_run.data_is_mock} />
                      </div>
                    </>
                  ) : (
                    <span className="muted">never</span>
                  )}
                </td>
                <td>
                  <select value={wl[s.name] ?? defaultWl(s.horizon) ?? ""} onChange={(e) => setWl({ ...wl, [s.name]: Number(e.target.value) })}>
                    {watchlists.data?.map((w) => (
                      <option key={w.id} value={w.id}>
                        {w.name} ({w.horizon})
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <button
                    disabled={busy || !owner || acct == null || s.paused || d.kill_switch_active}
                    title={!owner ? "owner role required" : undefined}
                    onClick={() => run(s.name, s.horizon)}
                  >
                    Run PAPER cycle now
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </>
  );
}
