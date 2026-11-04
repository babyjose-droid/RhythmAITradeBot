"use client";

import { ErrorBox, useData, when } from "@/components/ui";
import { api } from "@/lib/api";

interface Gate {
  id: string;
  area: string;
  title: string;
  status: "MET" | "NOT_MET" | "OWNER";
  evidence: string;
  blocking: boolean;
  source: string;
}

interface Review {
  generated_at: string;
  verdict: "NOT_READY" | "READY_FOR_OWNER_DECISION";
  live_trading_implemented: boolean;
  note: string;
  counts: Record<string, number>;
  blockers: number;
  gates: Gate[];
  earliest_evidence_date_if_real_paper_starts_now: string;
  min_real_paper_days: number;
}

const TONE: Record<string, string> = { MET: "ok", NOT_MET: "fail", OWNER: "reduce" };
const LABEL: Record<string, string> = { MET: "met", NOT_MET: "not met", OWNER: "needs you / external" };

export default function Readiness() {
  const r = useData(() => api.get<Review>("/v1/system/live-readiness"));
  const d = r.data;
  const areas = d ? [...new Set(d.gates.map((g) => g.area))] : [];
  return (
    <>
      <h1>Live-readiness review</h1>
      <p className="muted">
        A checklist of what must be true before live trading could even be proposed. <strong>Review only</strong>: this
        page changes nothing, and live trading stays locked in the code until you give a separate, explicit approval.
        Not legal advice.
      </p>
      <ErrorBox error={r.error} />
      {d && (
        <section className={`card ${d.verdict === "NOT_READY" ? "notif-critical" : ""}`}>
          <h2>
            <span className={`tone tone-${d.verdict === "NOT_READY" ? "fail" : "ok"}`}>
              {d.verdict === "NOT_READY" ? "NOT READY" : "READY FOR YOUR DECISION"}
            </span>{" "}
            {d.blockers} blocking item(s) open
          </h2>
          <p>
            {d.counts.MET ?? 0} met · {d.counts.NOT_MET ?? 0} not met · {d.counts.OWNER ?? 0} need you or an external
            confirmation · live order path implemented: <strong>{d.live_trading_implemented ? "yes" : "no (locked)"}</strong>
          </p>
          <p className="muted">
            Evaluated {when(d.generated_at)}. The real-data PAPER evaluation needs {d.min_real_paper_days} trading days; if
            it started today the earliest date would be {d.earliest_evidence_date_if_real_paper_starts_now}.
          </p>
        </section>
      )}
      {areas.map((a) => (
        <section key={a} className="card">
          <h2>{a}</h2>
          <table>
            <thead>
              <tr>
                <th>Gate</th>
                <th>Requirement</th>
                <th>Status</th>
                <th>Evidence / what is needed</th>
              </tr>
            </thead>
            <tbody>
              {d?.gates
                .filter((g) => g.area === a)
                .map((g) => (
                  <tr key={g.id}>
                    <td className="mono">{g.id}</td>
                    <td>
                      {g.title}
                      {!g.blocking && <span className="muted"> (informational)</span>}
                    </td>
                    <td>
                      <span className={`tone tone-${TONE[g.status] ?? "fail"}`}>{LABEL[g.status] ?? g.status}</span>
                    </td>
                    <td className="muted">
                      {g.evidence}
                      {g.source && <div className="mono">source: {g.source}</div>}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      ))}
    </>
  );
}
