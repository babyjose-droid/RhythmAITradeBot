"use client";

import { useState } from "react";

import { ErrorBox, MockTag, OrderBadge, money, useData, when } from "@/components/ui";
import {
  api,
  type CycleResult,
  type Fundamentals,
  type Order,
  type PaperAccount,
  type PaperAccountDetail,
  type Thesis,
  type Watchlist,
} from "@/lib/api";
import { bandPosition, describeRule, thesisTone } from "@/lib/status";

const STRATEGY = "long_term_quality_value";
const METRICS: [string, string, (v: number) => string][] = [
  ["pe", "P/E", (v) => v.toFixed(1)],
  ["roe", "ROE", (v) => `${(v * 100).toFixed(1)}%`],
  ["debt_to_equity", "Debt/equity", (v) => v.toFixed(2)],
  ["eps_growth_yoy", "EPS growth (YoY TTM)", (v) => `${(v * 100).toFixed(1)}%`],
  ["eps_ttm", "EPS (TTM)", (v) => `₹${v.toFixed(2)}`],
];

function Band({ weight, band }: { weight: number | null; band: [number, number, number] }) {
  const [min, target, max] = band;
  const scale = Math.max(max * 1.5, weight ?? 0, 0.01);
  const pos = (x: number) => `${Math.min(100, (x / scale) * 100)}%`;
  const where = weight == null ? "within" : bandPosition(weight, min, max);
  return (
    <div className="band" title={`band ${min * 100}–${max * 100} %, target ${target * 100} %`}>
      <div className="band-range" style={{ left: pos(min), width: `calc(${pos(max)} - ${pos(min)})` }} />
      <div className="band-target" style={{ left: pos(target) }} />
      {weight != null && <div className={`band-weight band-${where}`} style={{ left: pos(weight) }} />}
      <span className="band-label">
        {weight == null ? "—" : `${(weight * 100).toFixed(1)} %`} ({where}) · band {min * 100}–{max * 100} %
      </span>
    </div>
  );
}

function FundamentalsRow({ instrument }: { instrument: string }) {
  const [ex, sym] = instrument.split(":");
  const f = useData(() => api.get<Fundamentals>(`/v1/fundamentals/${ex}/${sym}?quarters=8`), [instrument]);
  if (f.error) return <tr><td>{instrument}</td><td colSpan={7} className="muted">{f.error}</td></tr>;
  const d = f.data?.derived ?? {};
  const last = f.data?.quarters.at(-1);
  return (
    <tr>
      <td>
        {instrument} <MockTag show={!!f.data?.is_mock} />
      </td>
      <td>{money(f.data?.price)}</td>
      {METRICS.map(([k, , fmt]) => {
        const m = d[k];
        const v = m?.value == null ? null : Number(m.value);
        return (
          <td key={k} title={m ? `${m.formula}\n${m.inputs.join(", ")}` : "not enough published quarters"}>
            {v == null ? "—" : fmt(v)}
          </td>
        );
      })}
      <td className="muted">
        {last ? (
          <>
            {last.period_end} · published {when(last.known_at)}
          </>
        ) : (
          "none published"
        )}
      </td>
    </tr>
  );
}

export default function LongTermWorkspace() {
  const watchlists = useData(() => api.get<Watchlist[]>("/v1/watchlists"));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const [wid, setWid] = useState<number | null>(null);
  const [aid, setAid] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [why, setWhy] = useState<CycleResult["diagnostics"] | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  const wl =
    watchlists.data?.find((w) => w.id === wid) ??
    watchlists.data?.find((w) => w.horizon === "LONG_TERM" || w.horizon === "ANY");
  const wlId = wl?.id ?? null;
  const acctId = aid ?? accounts.data?.[0]?.id ?? null;

  const theses = useData(
    () => (acctId == null ? Promise.resolve([]) : api.get<Thesis[]>(`/v1/theses?account_id=${acctId}&horizon=LONG_TERM`)),
    [acctId],
  );
  const detailThesis = useData(
    () => (open == null ? Promise.resolve(null) : api.get<Thesis>(`/v1/theses/${open}`)),
    [open],
  );
  const orders = useData(
    () =>
      acctId == null
        ? Promise.resolve([])
        : api.get<Order[]>(`/v1/paper/accounts/${acctId}/orders?horizon=LONG_TERM&limit=50`),
    [acctId],
  );
  const detail = useData(
    () => (acctId == null ? Promise.resolve(null) : api.get<PaperAccountDetail>(`/v1/paper/accounts/${acctId}`)),
    [acctId],
  );

  async function act(fn: () => Promise<string>) {
    setBusy(true);
    setErr(null);
    try {
      setMsg(await fn());
      await Promise.all([theses.refresh(), orders.refresh(), detail.refresh(), detailThesis.refresh()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const cycle = (paper: boolean) =>
    act(async () => {
      const path = paper && acctId != null ? `/v1/paper/accounts/${acctId}/cycle` : "/v1/research/cycle";
      const r = await api.post<CycleResult>(path, { watchlist_id: wlId, strategy: STRATEGY });
      setWhy(r.diagnostics);
      return `${r.mode} long-term cycle: ${r.signals.length} candidate(s), ${r.new_orders} new PAPER order(s), ${r.new_fills} fill(s)`;
    });

  const sync = (path: string, label: string) =>
    act(async () => {
      const r = await api.post<{ source: string; instruments: number }>(path, { watchlist_id: wlId });
      return `${label} synced from ${r.source} for ${r.instruments} instrument(s)`;
    });

  const positions = detail.data?.positions.filter((p) => p.horizon === "LONG_TERM") ?? [];

  return (
    <>
      <h1>Long-term portfolio</h1>
      <p className="muted">
        Evidence-based holdings: quality at a reasonable price, held inside an allocation band and reviewed on a
        schedule and whenever new results are published. Fundamentals are <strong>MOCK</strong> until a licensed data
        source is chosen; nothing here is investment advice.
      </p>
      <ErrorBox error={err ?? watchlists.error ?? accounts.error} />
      {msg && <div className="note">{msg}</div>}

      <section className="card controls">
        <label>
          Watchlist{" "}
          <select value={wlId ?? ""} onChange={(e) => setWid(Number(e.target.value))}>
            {watchlists.data?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name} ({w.horizon})
              </option>
            ))}
          </select>
        </label>
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
        <button disabled={busy || wlId == null} onClick={() => sync("/v1/fundamentals/sync", "Fundamentals")}>
          Sync fundamentals
        </button>
        <button disabled={busy || wlId == null} onClick={() => sync("/v1/events/sync", "Event calendar")}>
          Sync event calendar
        </button>
        <button disabled={busy || wlId == null} onClick={() => cycle(false)}>
          Research cycle (no orders)
        </button>
        <button className="primary" disabled={busy || wlId == null || acctId == null} onClick={() => cycle(true)}>
          Run PAPER long-term cycle
        </button>
      </section>

      <section className="card">
        <h2>Evidence (published fundamentals, as of now)</h2>
        <p className="muted">Hover a number to see its formula and the quarters it came from.</p>
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Price</th>
              {METRICS.map(([k, label]) => (
                <th key={k}>{label}</th>
              ))}
              <th>Latest quarter</th>
            </tr>
          </thead>
          <tbody>
            {wl?.items.map((i) => (
              <FundamentalsRow key={i.instrument} instrument={i.instrument} />
            ))}
          </tbody>
        </table>
      </section>

      {why && (
        <section className="card">
          <h2>Last cycle — why / why not</h2>
          <table>
            <tbody>
              {Object.entries(why).map(([k, d]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{String(d.action ?? d.reason ?? "—")}</td>
                  <td className="mono">
                    {d.entry_checks
                      ? Object.entries(d.entry_checks as Record<string, boolean>)
                          .map(([c, ok]) => `${ok ? "✓" : "✗"} ${c}`)
                          .join("  ")
                      : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card paper-frame">
        <h2>
          Theses &amp; allocation <span className="tag-paper">PAPER</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Status</th>
              <th>Why held</th>
              <th>Invalidated by</th>
              <th>Weight vs band</th>
              <th>Next review</th>
            </tr>
          </thead>
          <tbody>
            {theses.data?.map((t) => {
              const band = (t.last_check.band as string[] | undefined)?.map(Number) as
                | [number, number, number]
                | undefined;
              const w = t.last_check.weight == null ? null : Number(t.last_check.weight);
              return (
                <tr key={t.id} onClick={() => setOpen(open === t.id ? null : t.id)} style={{ cursor: "pointer" }}>
                  <td>
                    {t.instrument} <MockTag show={t.data_is_mock} />
                  </td>
                  <td>
                    <span className={`tone tone-${thesisTone(t.status)}`}>{t.status}</span>
                  </td>
                  <td className="muted">{t.rationale.join("; ")}</td>
                  <td className="muted">{t.invalidation.map(describeRule).join(" · ")}</td>
                  <td>{band ? <Band weight={w} band={band} /> : "—"}</td>
                  <td>{t.closed_at ? <span className="muted">{t.close_reason}</span> : when(t.next_review_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {theses.data?.length === 0 && <p className="muted">No long-term theses yet.</p>}
        {detailThesis.data && (
          <div className="card">
            <h2>Review &amp; valuation history — {detailThesis.data.instrument}</h2>
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Change</th>
                  <th>Reason</th>
                  <th>P/E</th>
                  <th>ROE</th>
                  <th>Weight</th>
                  <th>Quarter</th>
                </tr>
              </thead>
              <tbody>
                {detailThesis.data.events?.map((e, i) => {
                  const det = e.detail ?? {};
                  const m = (det.metrics ?? det.key_metrics ?? {}) as Record<string, { value: string | null }>;
                  return (
                    <tr key={i}>
                      <td>{when(e.at)}</td>
                      <td>
                        {e.from ?? "—"} → {e.to}
                      </td>
                      <td>{e.reason}</td>
                      <td>{m.pe?.value ? Number(m.pe.value).toFixed(1) : "—"}</td>
                      <td>{m.roe?.value ? `${(Number(m.roe.value) * 100).toFixed(1)}%` : "—"}</td>
                      <td>{det.weight ? `${(Number(det.weight) * 100).toFixed(1)}%` : "—"}</td>
                      <td>{String(det.latest_period ?? "")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card paper-frame">
        <h2>
          Long-term PAPER orders &amp; holdings <span className="tag-paper">PAPER</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Created</th>
              <th>Instrument</th>
              <th>Side / purpose</th>
              <th>Status</th>
              <th>Qty (filled)</th>
              <th>Avg fill</th>
            </tr>
          </thead>
          <tbody>
            {orders.data?.map((o) => (
              <tr key={o.id}>
                <td>{when(o.created_at)}</td>
                <td>{o.instrument}</td>
                <td>
                  {o.side} · {o.purpose}
                </td>
                <td>
                  <OrderBadge display={o.display} /> <span className="muted">{o.state}</span>
                </td>
                <td>
                  {o.quantity} ({o.filled_quantity})
                </td>
                <td>{money(o.average_fill_price)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table>
          <thead>
            <tr>
              <th>Holding</th>
              <th>Qty</th>
              <th>Avg price</th>
              <th>Since</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((p) => (
              <tr key={p.instrument}>
                <td>{p.instrument}</td>
                <td>{p.quantity}</td>
                <td>{money(p.average_price)}</td>
                <td>{when(p.opened_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {positions.length === 0 && <p className="muted">No long-term holdings.</p>}
      </section>
    </>
  );
}
