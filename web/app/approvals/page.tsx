"use client";

import { useState } from "react";

import { useMe } from "@/components/Session";
import { ErrorBox, MockTag, money, useData, when } from "@/components/ui";
import { api, type Approval } from "@/lib/api";
import { approvalTone, canAct } from "@/lib/status";

const FILTERS = ["PENDING", "APPROVED", "REJECTED", "EXPIRED", "FAILED", ""] as const;

export default function Approvals() {
  const me = useMe();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("PENDING");
  const list = useData(() => api.get<Approval[]>(`/v1/approvals?limit=100${filter ? `&status=${filter}` : ""}`), [filter]);
  const [reason, setReason] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const may = canAct(me?.role, "approve");

  async function decide(a: Approval, decision: "approve" | "reject") {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<Approval>(`/v1/approvals/${a.id}/decision`, {
        decision,
        reason: reason[a.id] ?? "",
      });
      setMsg(
        r.status === "APPROVED"
          ? `Approved — risk re-check passed; PAPER order ${r.order_id} created (not yet filled).`
          : r.status === "FAILED"
            ? `Not executed: ${r.decision_reason}`
            : `Request ${r.status.toLowerCase()}.`,
      );
      await list.refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Approvals</h1>
      <p className="muted">
        Only candidates marked <strong>REQUIRES REVIEW</strong> wait here — by the risk engine (order value above the
        review threshold) or by the AI research gate. Approving re-runs every risk check on current data; only if it
        passes is a PAPER order created. A request is an <strong>idea</strong>, not an order.
      </p>
      <ErrorBox error={err ?? list.error} />
      {msg && <div className="note">{msg}</div>}
      <section className="card controls">
        <label>
          Show{" "}
          <select value={filter} onChange={(e) => setFilter(e.target.value as (typeof FILTERS)[number])}>
            {FILTERS.map((f) => (
              <option key={f} value={f}>
                {f || "all"}
              </option>
            ))}
          </select>
        </label>
        {!may && <span className="muted">Your role ({me?.role ?? "—"}) can view but not decide.</span>}
      </section>
      {list.data?.length === 0 && <p className="muted">Nothing here.</p>}
      {list.data?.map((a) => (
        <section key={a.id} className="card paper-frame">
          <h2>
            #{a.id} {a.signal_type} {a.instrument} <MockTag show={a.data_is_mock} />{" "}
            <span className={`tone tone-${approvalTone(a.status)}`}>{a.status}</span>{" "}
            <span className="badge badge-candidate">IDEA — not an order</span>
          </h2>
          <p>
            <strong>{a.source === "RESEARCH" ? "Held by AI research:" : "Risk review:"}</strong> {a.reason}
          </p>
          <table>
            <tbody>
              <tr>
                <td>Strategy / horizon</td>
                <td>
                  {a.strategy} · {a.horizon}
                </td>
              </tr>
              <tr>
                <td>Decided by the rules</td>
                <td>
                  {when(a.detail.decided_at)} at {money(a.detail.ref_price)} · stop {money(a.detail.stop)} · target{" "}
                  {money(a.detail.target)}
                  {a.detail.target_weight && ` · target weight ${(Number(a.detail.target_weight) * 100).toFixed(1)}%`}
                </td>
              </tr>
              <tr>
                <td>Why</td>
                <td className="muted">{a.detail.reasons.join("; ")}</td>
              </tr>
              <tr>
                <td>Risk at the time</td>
                <td>
                  {a.detail.risk.outcome}, quantity {a.detail.risk.quantity} (policy {a.detail.risk.policy_version})
                  {a.detail.assessment_id && (
                    <>
                      {" "}
                      · <a href="/research">assessment #{a.detail.assessment_id}</a>
                    </>
                  )}
                </td>
              </tr>
              <tr>
                <td>Expires</td>
                <td>{when(a.expires_at)}</td>
              </tr>
              {a.decided_at && (
                <tr>
                  <td>Decision</td>
                  <td>
                    {when(a.decided_at)} by {a.decided_by}: {a.decision_reason}
                    {a.decision_risk && (
                      <span className="muted">
                        {" "}
                        · re-check {a.decision_risk.outcome} qty {a.decision_risk.quantity} at {money(a.decision_risk.price)} (
                        {a.decision_risk.quote_status})
                      </span>
                    )}
                    {a.order_id && <span> · PAPER order {a.order_id}</span>}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          {a.status === "PENDING" && may && (
            <div className="controls">
              <input
                placeholder="reason (required)"
                value={reason[a.id] ?? ""}
                onChange={(e) => setReason({ ...reason, [a.id]: e.target.value })}
                maxLength={500}
                style={{ minWidth: 260 }}
              />
              <button className="primary" disabled={busy || (reason[a.id] ?? "").trim().length < 3} onClick={() => decide(a, "approve")}>
                Approve (re-run risk, then PAPER order)
              </button>
              <button disabled={busy || (reason[a.id] ?? "").trim().length < 3} onClick={() => decide(a, "reject")}>
                Reject
              </button>
            </div>
          )}
        </section>
      ))}
    </>
  );
}
