"use client";

import { useState } from "react";

import { ErrorBox, useData, when } from "@/components/ui";
import { api, type JobRun, type PaperAccount, type ScheduledJob, type SchedulerStatus, type Watchlist } from "@/lib/api";
import { healthTone, runTone } from "@/lib/status";

const STRATEGY_SCHEDULE: Record<string, ScheduledJob["schedule"]> = {
  intraday_orb_vwap: "intraday_5m",
  swing_trend_pullback: "after_close",
  long_term_quality_value: "after_close",
};

function describe(j: ScheduledJob): string {
  switch (j.schedule) {
    case "intraday_5m":
      return "every 5 min, 09:20–15:30 IST, trading days";
    case "after_close":
      return "15:45 IST on trading days";
    case "daily_at":
      return `${j.schedule_arg} IST ${j.trading_days_only ? "on trading days" : "daily"}`;
    default:
      return `every ${j.schedule_arg} min ${j.trading_days_only ? "on trading days" : ""}`;
  }
}

export function SchedulerPanel({ owner }: { owner: boolean }) {
  const st = useData(() => api.get<SchedulerStatus>("/v1/scheduler/status"));
  const jobs = useData(() => api.get<ScheduledJob[]>("/v1/scheduler/jobs"));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const watchlists = useData(() => api.get<Watchlist[]>("/v1/watchlists"));
  const [open, setOpen] = useState<number | null>(null);
  const runs = useData(
    () => (open == null ? Promise.resolve([]) : api.get<JobRun[]>(`/v1/scheduler/jobs/${open}/runs?limit=20`)),
    [open],
  );
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    name: "",
    kind: "paper_cycle" as ScheduledJob["kind"],
    strategy: "long_term_quality_value",
    account_id: "",
    watchlist_id: "",
    schedule: "after_close" as ScheduledJob["schedule"],
    schedule_arg: "",
  });

  async function act(fn: () => Promise<string>) {
    setBusy(true);
    setErr(null);
    try {
      setMsg(await fn());
      await Promise.all([jobs.refresh(), st.refresh(), runs.refresh()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function params(): Record<string, unknown> {
    if (form.kind === "paper_cycle")
      return { strategy: form.strategy, account_id: Number(form.account_id), watchlist_id: Number(form.watchlist_id) };
    if (form.kind === "events_sync" || form.kind === "fundamentals_sync") return { watchlist_id: Number(form.watchlist_id) };
    return {};
  }

  const schedule = form.kind === "paper_cycle" ? STRATEGY_SCHEDULE[form.strategy] : form.schedule;
  const s = st.data;
  const leaderAge = s?.leader_heartbeat_age_seconds;
  const alive = leaderAge != null && leaderAge <= (s?.stale_after_seconds ?? 120);

  return (
    <>
      <section className="card">
        <h2>Scheduler &amp; health</h2>
        <ErrorBox error={err ?? st.error ?? jobs.error} />
        {msg && <div className="note">{msg}</div>}
        {s && (
          <p>
            <span className={`tone tone-${alive ? "ok" : s.expected ? "fail" : "skip"}`}>
              {alive ? "RUNNING" : s.expected ? "NOT RUNNING" : "not deployed"}
            </span>{" "}
            leader heartbeat {leaderAge == null ? "never" : `${Math.round(leaderAge)} s ago`} (stale after{" "}
            {s.stale_after_seconds} s) · {s.instances.length} instance(s) seen
          </p>
        )}
        <table>
          <tbody>
            {s?.health.map((h) => (
              <tr key={h.component}>
                <td>{h.component.replace("_", " ")}</td>
                <td>
                  <span className={`tone tone-${healthTone(h.status)}`}>{h.status.toUpperCase()}</span>
                </td>
                <td className="muted mono">{JSON.stringify(h.detail)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          Scheduled jobs <span className="tag-paper">PAPER</span>
        </h2>
        <p className="muted">
          New jobs start <strong>disabled</strong>. Enabling one, like creating it, asks you to confirm with your password
          and code. One run per slot; a job is switched off automatically after repeated failures.
        </p>
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>When</th>
              <th>Status</th>
              <th>Next run</th>
              <th>Last run</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.data?.map((j) => (
              <tr key={j.id}>
                <td onClick={() => setOpen(open === j.id ? null : j.id)} style={{ cursor: "pointer" }}>
                  <strong>{j.name}</strong>
                  <div className="muted mono">
                    {j.kind} {JSON.stringify(j.params)}
                  </div>
                </td>
                <td className="muted">{describe(j)}</td>
                <td>
                  <span className={`tone tone-${j.enabled ? "ok" : "skip"}`}>{j.enabled ? "enabled" : "disabled"}</span>
                  {j.disabled_reason && <div className="muted">{j.disabled_reason}</div>}
                </td>
                <td>{j.enabled ? when(j.next_run_at) : "—"}</td>
                <td>
                  <span className={`tone tone-${runTone(j.last_status)}`}>{j.last_status ?? "never"}</span>{" "}
                  <span className="muted">{when(j.last_run_at)}</span>
                  {j.last_error && <div className="muted">{j.last_error}</div>}
                </td>
                <td>
                  {owner && (
                    <>
                      <button
                        disabled={busy}
                        onClick={() =>
                          act(async () => {
                            await api.post(`/v1/scheduler/jobs/${j.id}/${j.enabled ? "disable" : "enable"}`, {
                              reason: j.enabled ? "disabled from the dashboard" : "enabled from the dashboard",
                            });
                            return `${j.name} ${j.enabled ? "disabled" : "enabled"}`;
                          })
                        }
                      >
                        {j.enabled ? "Disable" : "Enable"}
                      </button>{" "}
                      <button
                        disabled={busy}
                        onClick={() =>
                          act(async () => {
                            const r = await api.post<{ status: string }>(`/v1/scheduler/jobs/${j.id}/run`);
                            setOpen(j.id);
                            return `${j.name}: ${r.status}`;
                          })
                        }
                      >
                        Run now
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {jobs.data?.length === 0 && <p className="muted">No scheduled jobs yet.</p>}
        {open != null && (
          <>
            <h3>Recent runs</h3>
            <table>
              <thead>
                <tr>
                  <th>Slot</th>
                  <th>Trigger</th>
                  <th>Status</th>
                  <th>Duration</th>
                  <th>Instance</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {runs.data?.map((r) => (
                  <tr key={r.id}>
                    <td>{when(r.slot)}</td>
                    <td>{r.trigger}</td>
                    <td>
                      <span className={`tone tone-${runTone(r.status)}`}>{r.status}</span>
                    </td>
                    <td>{r.duration_ms != null ? `${r.duration_ms} ms` : "—"}</td>
                    <td className="muted">{r.instance}</td>
                    <td className="muted mono">{JSON.stringify(r.detail).slice(0, 220)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
        {owner && (
          <form
            className="form"
            onSubmit={(e) => {
              e.preventDefault();
              void act(async () => {
                await api.post("/v1/scheduler/jobs", {
                  name: form.name,
                  kind: form.kind,
                  params: params(),
                  schedule,
                  schedule_arg: form.schedule_arg || null,
                  trading_days_only: true,
                });
                return `Job "${form.name}" created (disabled)`;
              });
            }}
          >
            <h3>Add a job</h3>
            <label>
              Name
              <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required minLength={3} />
            </label>
            <label>
              Kind
              <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as ScheduledJob["kind"] })}>
                <option value="paper_cycle">PAPER cycle</option>
                <option value="events_sync">event calendar sync</option>
                <option value="fundamentals_sync">fundamentals sync</option>
                <option value="health_check">health check (alarms)</option>
                <option value="audit_verify">audit chain verification</option>
              </select>
            </label>
            {form.kind === "paper_cycle" && (
              <>
                <label>
                  Strategy
                  <select value={form.strategy} onChange={(e) => setForm({ ...form, strategy: e.target.value })}>
                    {Object.keys(STRATEGY_SCHEDULE).map((k) => (
                      <option key={k}>{k}</option>
                    ))}
                  </select>
                </label>
                <label>
                  PAPER account
                  <select value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })} required>
                    <option value="">—</option>
                    {accounts.data?.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name}
                      </option>
                    ))}
                  </select>
                </label>
              </>
            )}
            {form.kind !== "health_check" && form.kind !== "audit_verify" && (
              <label>
                Watchlist
                <select value={form.watchlist_id} onChange={(e) => setForm({ ...form, watchlist_id: e.target.value })} required>
                  <option value="">—</option>
                  {watchlists.data?.map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name} ({w.horizon})
                    </option>
                  ))}
                </select>
              </label>
            )}
            {form.kind === "paper_cycle" ? (
              <p className="muted">Schedule: {schedule === "intraday_5m" ? "every 5 minutes in the session" : "15:45 IST after the close"}</p>
            ) : (
              <label>
                Schedule
                <select value={form.schedule} onChange={(e) => setForm({ ...form, schedule: e.target.value as ScheduledJob["schedule"] })}>
                  <option value="after_close">after the close (15:45 IST)</option>
                  <option value="daily_at">daily at (HH:MM IST)</option>
                  <option value="every_minutes">every N minutes</option>
                </select>
              </label>
            )}
            {form.kind !== "paper_cycle" && (form.schedule === "daily_at" || form.schedule === "every_minutes") && (
              <label>
                {form.schedule === "daily_at" ? "Time (HH:MM IST)" : "Minutes"}
                <input value={form.schedule_arg} onChange={(e) => setForm({ ...form, schedule_arg: e.target.value })} required />
              </label>
            )}
            <div className="row">
              <button className="primary" disabled={busy}>
                Create (disabled)
              </button>
            </div>
          </form>
        )}
      </section>
    </>
  );
}
