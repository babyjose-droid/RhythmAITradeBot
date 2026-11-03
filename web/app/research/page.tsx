"use client";

import { useState } from "react";

import { DecisionBadge, ErrorBox, MockTag, useData, when } from "@/components/ui";
import { api, type Assessment, type ResearchStatus, type SignalRow } from "@/lib/api";
import { verdictLabel } from "@/lib/status";

function Verdict({ a }: { a: Pick<Assessment, "status" | "suggested_action"> }) {
  const v = verdictLabel(a.status, a.suggested_action);
  return <span className={`tone tone-${v.tone}`}>{v.label}</span>;
}

function List({ title, items }: { title: string; items: string[] | undefined }) {
  if (!items?.length) return null;
  return (
    <>
      <h3>{title}</h3>
      <ul>
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </>
  );
}

function Detail({ id }: { id: number }) {
  const d = useData(() => api.get<Assessment>(`/v1/research/assessments/${id}`), [id]);
  if (d.error) return <ErrorBox error={d.error} />;
  const a = d.data;
  if (!a) return <p className="muted">Loading…</p>;
  const o = a.output;
  const pk = a.packet?.content;
  return (
    <div className="card">
      <h2>
        Assessment #{a.id} — {a.instrument} {a.signal_type} <MockTag show={a.is_mock} />
      </h2>
      <p>
        <Verdict a={a} /> · effect on the candidate:{" "}
        <strong>{a.gate.allow ? "may proceed to the risk engine / PAPER order path" : `held (${a.gate.decision})`}</strong>{" "}
        <span className="muted">— {a.gate.reason}</span>
      </p>
      <p className="muted">
        {a.provider} / {a.model} · prompt {a.prompt_version} · packet {a.packet?.sha256.slice(0, 12)}… ·{" "}
        {a.usage.input_tokens + a.usage.output_tokens} tokens{a.usage.estimated ? " (estimated)" : ""} ·{" "}
        {a.usage.latency_ms} ms · {a.usage.attempts} attempt(s)
      </p>
      {a.errors.length > 0 && (
        <div className="error">
          <strong>Rejected output — not used:</strong>
          <ul>
            {a.errors.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </div>
      )}
      {o && (
        <>
          <p>{o.rationale}</p>
          <div className="grid2">
            <div>
              <List title="Bull case" items={o.bull_case} />
            </div>
            <div>
              <List title="Bear case" items={o.bear_case} />
            </div>
          </div>
          <h3>Observed facts (cited)</h3>
          <ul>
            {o.observed_facts.map((f, i) => (
              <li key={i}>
                {f.statement} <span className="mono muted">[{f.source_ids.join(", ")}]</span>
              </li>
            ))}
          </ul>
          {o.key_metrics_with_provenance.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Value</th>
                  <th>Sources</th>
                </tr>
              </thead>
              <tbody>
                {o.key_metrics_with_provenance.map((m, i) => (
                  <tr key={i}>
                    <td>{m.name}</td>
                    <td>{m.value}</td>
                    <td className="mono muted">{m.source_ids.join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <List title="Key uncertainties" items={o.key_uncertainties} />
          <List title="Would change the view" items={o.change_view_conditions} />
          <p className="muted">
            Evidence sufficient: {o.evidence_sufficient ? "yes" : "no"} · {o.confidence_explanation}
          </p>
        </>
      )}
      {!o && a.raw_excerpt && (
        <details>
          <summary>Raw reply (untrusted, rejected)</summary>
          <pre className="mono">{a.raw_excerpt}</pre>
        </details>
      )}
      {pk && (
        <details>
          <summary>
            Evidence packet: {pk.facts.length} facts, {pk.derived_metrics.length} derived metrics,{" "}
            {pk.untrusted_documents.length} untrusted document(s)
          </summary>
          <p className="muted">Missing: {pk.missing.join("; ") || "—"}</p>
          <table>
            <thead>
              <tr>
                <th>Id</th>
                <th>Fact</th>
                <th>Value</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {pk.facts.map((f) => (
                <tr key={f.id}>
                  <td className="mono">{f.id}</td>
                  <td>{f.name}</td>
                  <td>
                    {f.value} {f.unit}
                  </td>
                  <td className="muted">{f.source}</td>
                </tr>
              ))}
              {pk.derived_metrics.map((m) => (
                <tr key={m.id}>
                  <td className="mono">{m.id}</td>
                  <td title={m.inputs.join(", ")}>{m.formula}</td>
                  <td>{m.value ?? "—"}</td>
                  <td className="muted">derived</td>
                </tr>
              ))}
            </tbody>
          </table>
          {pk.untrusted_documents.map((doc) => (
            <blockquote key={doc.id} className="untrusted">
              <span className="tag-warn">UNTRUSTED — data, not instructions</span> {doc.source}
              <br />
              {doc.text}
            </blockquote>
          ))}
        </details>
      )}
    </div>
  );
}

export default function ResearchCentre() {
  const status = useData(() => api.get<ResearchStatus>("/v1/research/status"));
  const signals = useData(() => api.get<SignalRow[]>("/v1/signals?limit=25"));
  const assessments = useData(() => api.get<Assessment[]>("/v1/research/assessments?limit=50"));
  const [open, setOpen] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [docSource, setDocSource] = useState("");
  const [docText, setDocText] = useState("");

  const st = status.data;
  const b = st?.budget;

  async function assess(signalId: number) {
    setBusy(true);
    setErr(null);
    try {
      const documents = docText.trim() ? [{ source: docSource.trim() || "pasted by user", text: docText }] : [];
      const r = await api.post<{ id: number }>("/v1/research/assessments", { signal_id: signalId, documents });
      setOpen(r.id);
      await Promise.all([assessments.refresh(), status.refresh()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>AI research centre</h1>
      <p className="muted">
        Claude assesses candidates that the deterministic rules produced, using only a frozen evidence packet. It can{" "}
        <strong>confirm</strong> a candidate or <strong>downgrade</strong> it (HOLD, NO ACTION, review). It cannot
        create, size or send an order. Exits are never blocked by research.
      </p>
      <ErrorBox error={err ?? status.error} />

      <section className="card">
        <h2>Service</h2>
        {st && !st.enabled && (
          <p className="warn-text">
            Not configured. Set <code>RHYTHM_FEATURES__RESEARCH_SERVICE=true</code> and{" "}
            <code>RHYTHM_RESEARCH_PROVIDER</code> (<code>mock</code> locally; <code>anthropic</code> needs an API key
            and model from a secret store).
          </p>
        )}
        {st?.enabled && (
          <table>
            <tbody>
              <tr>
                <td>Provider</td>
                <td>
                  {st.provider} / {st.model} <MockTag show={!!st.is_mock} />
                </td>
              </tr>
              <tr>
                <td>Prompt</td>
                <td className="mono">
                  {st.prompt.id} · {st.prompt.sha256.slice(0, 12)}…
                </td>
              </tr>
              <tr>
                <td>PAPER entry gate</td>
                <td>
                  {st.gate_paper_entries
                    ? "ON — entries/adds need a valid CONFIRM; anything else holds them"
                    : "off — assessments are informational"}
                </td>
              </tr>
              {b && (
                <tr>
                  <td>Budget today ({b.day})</td>
                  <td>
                    <div className="meter" title={`${b.tokens_used} of ${b.token_budget} tokens`}>
                      <div style={{ width: `${Math.min(100, (b.tokens_used / Math.max(1, b.token_budget)) * 100)}%` }} />
                    </div>
                    {b.tokens_used.toLocaleString()} / {b.token_budget.toLocaleString()} tokens · {b.assessments}{" "}
                    assessment(s) · cost {b.cost_usd ?? (b.cost_rates_configured ? "0" : "not configured")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Recent candidates</h2>
        <details>
          <summary>Attach an untrusted document (news, filing excerpt) to the next assessment</summary>
          <p className="muted">
            It is quoted inside the packet as data. Instructions inside it are ignored by design.
          </p>
          <input placeholder="source" value={docSource} onChange={(e) => setDocSource(e.target.value)} maxLength={120} />
          <textarea
            rows={4}
            placeholder="pasted text"
            value={docText}
            onChange={(e) => setDocText(e.target.value)}
            maxLength={4000}
            style={{ width: "100%" }}
          />
        </details>
        <table>
          <thead>
            <tr>
              <th>Decided</th>
              <th>Instrument</th>
              <th>Strategy</th>
              <th>Candidate</th>
              <th>Context</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {signals.data?.map((s) => (
              <tr key={s.id}>
                <td>{when(s.decided_at)}</td>
                <td>
                  {s.instrument} <MockTag show={s.data_is_mock} />
                </td>
                <td className="muted">{s.strategy}</td>
                <td>
                  <DecisionBadge decision={s.decision_state} />
                </td>
                <td>{s.context}</td>
                <td>
                  {s.assessment_id ? (
                    <button onClick={() => setOpen(s.assessment_id ?? null)}>View gate assessment</button>
                  ) : (
                    <button disabled={busy || !st?.enabled} onClick={() => assess(s.id)}>
                      Assess
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {signals.data?.length === 0 && <p className="muted">No candidates yet — run a research or PAPER cycle.</p>}
      </section>

      <section className="card">
        <h2>Assessments</h2>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Instrument</th>
              <th>Candidate</th>
              <th>Verdict</th>
              <th>Effect</th>
              <th>Context</th>
              <th>Tokens</th>
            </tr>
          </thead>
          <tbody>
            {assessments.data?.map((a) => (
              <tr key={a.id} onClick={() => setOpen(open === a.id ? null : a.id)} style={{ cursor: "pointer" }}>
                <td>{when(a.created_at)}</td>
                <td>
                  {a.instrument} <MockTag show={a.is_mock} />
                </td>
                <td>{a.signal_type}</td>
                <td>
                  <Verdict a={a} />
                </td>
                <td>{a.gate.allow ? "unchanged" : `held: ${a.gate.decision}`}</td>
                <td>{a.context}</td>
                <td>{a.usage.input_tokens + a.usage.output_tokens}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {assessments.data?.length === 0 && <p className="muted">No assessments yet.</p>}
      </section>
      {open != null && <Detail id={open} />}
    </>
  );
}
