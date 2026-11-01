"use client";

import { useState } from "react";

import { ErrorBox, Tone, useData, when } from "@/components/ui";
import { api, CONTROLS_CHANGED, type Controls, type Policies, type RiskCheck } from "@/lib/api";

const STRATEGY = "intraday_orb_vwap";

export default function RiskControls() {
  const controls = useData(() => api.get<Controls>("/v1/risk/controls?history=20"));
  const policies = useData(() => api.get<Policies>("/v1/risk/policies"));
  const checks = useData(() => api.get<RiskCheck[]>("/v1/risk/checks?limit=40"));
  const [reason, setReason] = useState("");
  const [flatten, setFlatten] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    await Promise.all([controls.refresh(), policies.refresh(), checks.refresh()]);
  };

  async function act(path: string, body: Record<string, unknown>, done: string) {
    setBusy(true);
    setErr(null);
    try {
      await api.post(path, body);
      window.dispatchEvent(new Event(CONTROLS_CHANGED));
      setMsg(done);
      setReason("");
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const c = controls.data;
  const paused = c ? Object.entries(c.paused_strategies) : [];
  const needReason = reason.trim().length < 3;

  return (
    <>
      <h1>Risk &amp; controls</h1>
      <ErrorBox error={err ?? controls.error ?? policies.error ?? checks.error} />
      {msg && <div className="note">{msg}</div>}

      <section className={`card ${c?.kill_switch_active ? "paper-frame" : ""}`}>
        <h2>
          Kill switch{" "}
          {c && (
            <span className={`tone ${c.kill_switch_active ? "tone-fail" : "tone-ok"}`}>
              {c.kill_switch_active ? "ACTIVE" : "OFF"}
            </span>
          )}
        </h2>
        {c?.kill_switch && (
          <p className="muted">
            Last change {when(c.kill_switch.at)} by {c.kill_switch.actor_type}:{c.kill_switch.actor_id} — “
            {c.kill_switch.reason}”
          </p>
        )}
        <p className="muted">
          Activating blocks every new risk-increasing order and cancels open entry orders. Protective
          stops/targets stay in place. Release always needs a person and a reason.
        </p>
        <label>
          Reason (required)
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
        </label>
        <div className="controls">
          {!c?.kill_switch_active ? (
            <>
              <label>
                <input type="checkbox" checked={flatten} onChange={(e) => setFlatten(e.target.checked)} /> also
                flatten intraday PAPER positions
              </label>
              <button
                className="danger"
                disabled={busy || needReason}
                onClick={() =>
                  act(
                    "/v1/risk/kill-switch",
                    { action: "activate", reason, flatten_intraday: flatten },
                    "Kill switch activated",
                  )
                }
              >
                Activate kill switch
              </button>
            </>
          ) : (
            <button
              disabled={busy || needReason}
              onClick={() => act("/v1/risk/kill-switch", { action: "release", reason }, "Kill switch released")}
            >
              Release kill switch
            </button>
          )}
          <button
            disabled={busy || needReason}
            onClick={() =>
              paused.some(([n]) => n === STRATEGY)
                ? act(`/v1/strategies/${STRATEGY}/resume`, { reason }, "Strategy resumed")
                : act(`/v1/strategies/${STRATEGY}/pause`, { reason }, "Strategy paused")
            }
          >
            {paused.some(([n]) => n === STRATEGY) ? "Resume" : "Pause"} {STRATEGY}
          </button>
        </div>
        {paused.length > 0 && (
          <table>
            <thead>
              <tr>
                <th>Paused strategy</th>
                <th>Since</th>
                <th>By</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {paused.map(([name, e]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td>{when(e.at)}</td>
                  <td>
                    {e.actor_type}:{e.actor_id}
                  </td>
                  <td>{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <details>
          <summary>Control history</summary>
          <table>
            <tbody>
              {c?.history.map((h) => (
                <tr key={h.id}>
                  <td>{when(h.at)}</td>
                  <td>
                    {h.control} {h.target !== "*" ? h.target : ""}
                  </td>
                  <td>{h.action}</td>
                  <td>
                    {h.actor_type}:{h.actor_id}
                  </td>
                  <td className="muted">{h.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </section>

      <section className="card">
        <h2>
          Active risk policy {policies.data && <span className="mono">{policies.data.active.version}</span>}
        </h2>
        <p className="muted">{policies.data?.note}</p>
        <table>
          <tbody>
            {policies.data &&
              Object.entries(policies.data.active)
                .filter(([k]) => k !== "version")
                .map(([k, v]) => (
                  <tr key={k}>
                    <td>{k}</td>
                    <td className="mono">{typeof v === "object" ? JSON.stringify(v) : String(v)}</td>
                  </tr>
                ))}
          </tbody>
        </table>
      </section>

      <section className="card">
        <h2>Recent risk decisions</h2>
        <table>
          <thead>
            <tr>
              <th>Decided</th>
              <th>Instrument</th>
              <th>Intent / purpose</th>
              <th>Outcome</th>
              <th>Qty</th>
              <th>Failed / reduced checks</th>
              <th>Order</th>
            </tr>
          </thead>
          <tbody>
            {checks.data?.map((r) => {
              const flagged = r.checks.filter((x) => x.status === "FAIL" || x.status === "REDUCE");
              return (
                <tr key={r.id}>
                  <td>{when(r.decided_at)}</td>
                  <td>
                    {r.instrument} <span className="muted">{r.context}</span>
                  </td>
                  <td>
                    {r.intent} · {r.purpose}
                  </td>
                  <td>
                    <Tone value={r.outcome} kind="outcome" />
                  </td>
                  <td>{r.quantity}</td>
                  <td>
                    <details>
                      <summary>
                        {flagged.length === 0
                          ? "all applicable checks passed"
                          : flagged.map((x) => `${x.code} ${x.status}`).join(", ")}
                      </summary>
                      <table>
                        <tbody>
                          {r.checks.map((x) => (
                            <tr key={x.code}>
                              <td>{x.code}</td>
                              <td>{x.name}</td>
                              <td>
                                <Tone value={x.status} />
                              </td>
                              <td className="muted">{x.detail}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </details>
                  </td>
                  <td className="mono">{r.order_id ?? "none"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </>
  );
}
