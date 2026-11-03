"use client";

import { Fragment, useState } from "react";

import { ErrorBox, useData, when } from "@/components/ui";
import { api, type AuditEventRow, type PaperAccount, type Reconciliation } from "@/lib/api";

interface Verify {
  ok: boolean;
  rows_checked: number;
  first_bad_id: number | null;
  reason: string | null;
}

const GROUPS: [string, string][] = [
  ["", "all events"],
  ["auth.*", "sign-ins & re-auth"],
  ["user.*", "users"],
  ["approval.*", "approvals"],
  ["risk.*", "risk controls & policies"],
  ["paper.*", "PAPER cycles & orders"],
  ["research.*", "AI research"],
  ["reconciliation.*", "reconciliation"],
];

export default function Audit() {
  const [type, setType] = useState("");
  const [actorId, setActorId] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const q = new URLSearchParams({ limit: "100" });
  if (type) q.set("event_type", type);
  if (actorId.trim()) q.set("actor_id", actorId.trim());
  if (before) q.set("before_id", String(before));
  const events = useData(() => api.get<AuditEventRow[]>(`/v1/audit/events?${q.toString()}`), [type, actorId, before]);
  const verify = useData(() => api.get<Verify>("/v1/audit/verify"));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const [aid, setAid] = useState<number | null>(null);
  const acct = aid ?? accounts.data?.[0]?.id ?? null;
  const recs = useData(
    () => (acct == null ? Promise.resolve([]) : api.get<Reconciliation[]>(`/v1/reconciliations?account_id=${acct}&limit=30`)),
    [acct],
  );
  const [open, setOpen] = useState<number | null>(null);
  const v = verify.data;

  return (
    <>
      <h1>Audit &amp; reconciliation</h1>
      <ErrorBox error={events.error ?? verify.error} />
      <section className="card">
        <h2>Audit chain integrity</h2>
        {v ? (
          <p>
            <span className={`tone tone-${v.ok ? "ok" : "fail"}`}>{v.ok ? "INTACT" : "BROKEN"}</span> — {v.rows_checked}{" "}
            append-only, hash-chained events checked
            {!v.ok && ` · first bad row ${v.first_bad_id}: ${v.reason}`}{" "}
            <button onClick={() => void verify.refresh()}>Re-verify</button>
          </p>
        ) : (
          <p className="muted">Checking…</p>
        )}
      </section>

      <section className="card">
        <h2>Events</h2>
        <div className="filters">
          <label>
            Type
            <select
              value={type}
              onChange={(e) => {
                setBefore(null);
                setType(e.target.value);
              }}
            >
              {GROUPS.map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Actor
            <input value={actorId} placeholder="username or system id" onChange={(e) => setActorId(e.target.value)} />
          </label>
          <button disabled={!events.data?.length} onClick={() => setBefore(events.data?.at(-1)?.id ?? null)}>
            Older →
          </button>
          {before && <button onClick={() => setBefore(null)}>Newest</button>}
        </div>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>When</th>
              <th>Event</th>
              <th>Actor</th>
              <th>Subject</th>
            </tr>
          </thead>
          <tbody>
            {events.data?.map((e) => (
              <Fragment key={e.id}>
                <tr onClick={() => setOpen(open === e.id ? null : e.id)} style={{ cursor: "pointer" }}>
                  <td className="mono">{e.id}</td>
                  <td>{when(e.occurred_at)}</td>
                  <td className="mono">{e.event_type}</td>
                  <td>
                    {e.actor_id} <span className="muted">({e.actor_type})</span>
                  </td>
                  <td className="muted">{e.subject_type ? `${e.subject_type}:${e.subject_id}` : "—"}</td>
                </tr>
                {open === e.id && (
                  <tr>
                    <td colSpan={5}>
                      <pre className="mono">{JSON.stringify(e.payload, null, 2)}</pre>
                      <span className="muted mono">row hash {e.row_hash}</span>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          Reconciliation history <span className="tag-paper">PAPER</span>
        </h2>
        <label>
          Account{" "}
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
              <th>When</th>
              <th>Trigger</th>
              <th>Result</th>
              <th>Differences</th>
            </tr>
          </thead>
          <tbody>
            {recs.data?.map((r) => (
              <tr key={r.id}>
                <td>{when(r.run_at)}</td>
                <td>{r.trigger}</td>
                <td>
                  <span className={`tone tone-${r.status === "OK" ? "ok" : "fail"}`}>{r.status}</span>
                </td>
                <td className="muted">
                  {r.diffs.length === 0
                    ? "none"
                    : r.diffs.map((d) => `${d.kind} ${d.subject}: expected ${d.expected}, actual ${d.actual}`).join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {recs.data?.length === 0 && <p className="muted">No reconciliations yet.</p>}
      </section>
    </>
  );
}
