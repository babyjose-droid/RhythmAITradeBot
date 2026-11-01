"use client";

import { useState } from "react";

import { DecisionBadge, ErrorBox, MockTag, OrderBadge, money, useData, when } from "@/components/ui";
import {
  api,
  type CycleResult,
  type EventListing,
  type Order,
  type PaperAccount,
  type PaperAccountDetail,
  type SignalRow,
  type Thesis,
  type Watchlist,
} from "@/lib/api";
import { describeRule, thesisTone } from "@/lib/status";

const STRATEGY = "swing_trend_pullback";

export default function SwingWorkspace() {
  const watchlists = useData(() => api.get<Watchlist[]>("/v1/watchlists"));
  const accounts = useData(() => api.get<PaperAccount[]>("/v1/paper/accounts"));
  const [wid, setWid] = useState<number | null>(null);
  const [aid, setAid] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const wlId = wid ?? watchlists.data?.find((w) => w.horizon === "SWING" || w.horizon === "ANY")?.id ?? null;
  const acctId = aid ?? accounts.data?.[0]?.id ?? null;

  const events = useData(
    () => (wlId == null ? Promise.resolve(null) : api.get<EventListing>(`/v1/events?watchlist_id=${wlId}&days=90`)),
    [wlId],
  );
  const theses = useData(
    () => (acctId == null ? Promise.resolve([]) : api.get<Thesis[]>(`/v1/theses?account_id=${acctId}&limit=50`)),
    [acctId],
  );
  const orders = useData(
    () =>
      acctId == null ? Promise.resolve([]) : api.get<Order[]>(`/v1/paper/accounts/${acctId}/orders?horizon=SWING&limit=50`),
    [acctId],
  );
  const detail = useData(
    () => (acctId == null ? Promise.resolve(null) : api.get<PaperAccountDetail>(`/v1/paper/accounts/${acctId}`)),
    [acctId],
  );
  const signals = useData(
    () =>
      api.get<SignalRow[]>(
        acctId == null ? "/v1/signals?horizon=SWING&limit=30" : `/v1/signals?horizon=SWING&account_id=${acctId}&limit=30`,
      ),
    [acctId],
  );

  const refreshAll = async () => {
    await Promise.all([events.refresh(), theses.refresh(), orders.refresh(), detail.refresh(), signals.refresh()]);
  };

  async function act(fn: () => Promise<string>) {
    setBusy(true);
    setErr(null);
    try {
      setMsg(await fn());
      await refreshAll();
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
      const blocked = Object.values(r.skipped);
      return (
        `${r.mode} swing cycle: ${r.signals.length} candidate(s), ${r.new_orders} new PAPER order(s), ` +
        `${r.new_fills} simulated fill(s)` +
        (blocked.length ? ` — ${blocked.join("; ")}` : "")
      );
    });

  const swingPositions = detail.data?.positions.filter((p) => p.horizon === "SWING") ?? [];

  return (
    <>
      <h1>Swing workspace</h1>
      <p className="muted">
        Daily bars, multi-day PAPER positions. Run cycles after the close: decisions use the completed session and
        fill at the next open. Each entry carries a thesis that is checked every session.
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
        <button
          disabled={busy || wlId == null}
          onClick={() =>
            act(async () => {
              const r = await api.post<{ source: string; instruments: number }>("/v1/events/sync", { watchlist_id: wlId });
              return `Event calendar synced from ${r.source} for ${r.instruments} instrument(s)`;
            })
          }
        >
          Sync event calendar
        </button>
        <button disabled={busy || wlId == null} onClick={() => cycle(false)}>
          Run research swing cycle (no orders)
        </button>
        <button className="primary" disabled={busy || wlId == null || acctId == null} onClick={() => cycle(true)}>
          Run PAPER swing cycle
        </button>
        <button onClick={refreshAll}>Refresh</button>
      </section>

      <section className="card">
        <h2>Event calendar (overnight risk)</h2>
        <p className="muted">
          New swing entries are blocked when the calendar has not been synced in the last 7 days or an event is within 3
          days (risk check R18). Only events already public at decision time are used.
        </p>
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Calendar last synced</th>
            </tr>
          </thead>
          <tbody>
            {events.data &&
              Object.entries(events.data.last_sync).map(([k, v]) => (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{v ? when(v) : <span className="tone tone-fail">never — entries blocked</span>}</td>
                </tr>
              ))}
          </tbody>
        </table>
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>In</th>
              <th>Instrument</th>
              <th>Event</th>
              <th>Source</th>
            </tr>
          </thead>
          <tbody>
            {events.data?.events.map((e) => (
              <tr key={`${e.instrument}-${e.kind}-${e.event_date}-${e.source}`}>
                <td>{e.event_date}</td>
                <td>{e.days_away} d</td>
                <td>{e.instrument}</td>
                <td>{e.kind}</td>
                <td>
                  {e.source} <MockTag show={e.is_mock} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {events.data?.events.length === 0 && <p className="muted">No known events in the next 90 days.</p>}
      </section>

      <section className="card paper-frame">
        <h2>
          Theses <span className="tag-paper">PAPER</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Status</th>
              <th>Why</th>
              <th>Invalidated by</th>
              <th>Last check</th>
              <th>Next review / closed</th>
            </tr>
          </thead>
          <tbody>
            {theses.data?.map((t) => {
              const reasons = (t.last_check.exit_reasons as string[] | undefined) ?? [];
              return (
                <tr key={t.id}>
                  <td>
                    {t.instrument} <MockTag show={t.data_is_mock} />
                    <div className="muted">
                      ref {money(t.ref_price)} · stop {money(t.stop)} · target {money(t.target)}
                    </div>
                  </td>
                  <td>
                    <span className={`tone tone-${thesisTone(t.status)}`}>{t.status}</span>
                  </td>
                  <td className="muted">{t.rationale.join("; ")}</td>
                  <td className="muted">{t.invalidation.map(describeRule).join(" · ")}</td>
                  <td>
                    {t.last_checked_at ? when(t.last_checked_at) : "—"}
                    {reasons.length > 0 && <div className="error">{reasons.join("; ")}</div>}
                    {t.last_check.held_sessions != null && (
                      <div className="muted">held {String(t.last_check.held_sessions)} session(s)</div>
                    )}
                  </td>
                  <td>
                    {t.closed_at ? (
                      <>
                        {when(t.closed_at)}
                        <div className="muted">{t.close_reason}</div>
                      </>
                    ) : (
                      when(t.next_review_at)
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {theses.data?.length === 0 && <p className="muted">No theses yet.</p>}
      </section>

      <section className="card">
        <h2>Swing candidates (ideas — not orders)</h2>
        <table>
          <thead>
            <tr>
              <th>Decided</th>
              <th>Instrument</th>
              <th>Status</th>
              <th>Ref price</th>
              <th>Linked PAPER order</th>
            </tr>
          </thead>
          <tbody>
            {signals.data?.map((s) => (
              <tr key={s.id}>
                <td>{when(s.decided_at)}</td>
                <td>
                  {s.instrument} <MockTag show={s.data_is_mock} />
                </td>
                <td>
                  <DecisionBadge decision={s.decision_state} /> <span className="muted">{s.context}</span>
                </td>
                <td>{money(s.ref_price)}</td>
                <td className="mono">{s.order_id ?? "none"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="card paper-frame">
        <h2>
          Swing PAPER orders &amp; positions <span className="tag-paper">PAPER</span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>Created</th>
              <th>Instrument</th>
              <th>Side / type / purpose</th>
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
                  {o.side} {o.order_type} · {o.purpose}
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
              <th>Position</th>
              <th>Qty</th>
              <th>Avg price</th>
              <th>Stop</th>
              <th>Target</th>
              <th>Since</th>
            </tr>
          </thead>
          <tbody>
            {swingPositions.map((p) => (
              <tr key={p.instrument}>
                <td>{p.instrument}</td>
                <td>{p.quantity}</td>
                <td>{money(p.average_price)}</td>
                <td>{money(p.stop)}</td>
                <td>{money(p.target)}</td>
                <td>{when(p.opened_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {swingPositions.length === 0 && <p className="muted">No open swing positions.</p>}
      </section>
    </>
  );
}
