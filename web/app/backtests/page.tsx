"use client";

import { useState } from "react";

import { ErrorBox, MockTag, useData, when } from "@/components/ui";
import { api, type BacktestRun } from "@/lib/api";

const SHOWN = [
  "total_return_pct",
  "benchmark_equal_weight_buy_hold_pct",
  "max_drawdown_pct",
  "annualised_volatility_pct",
  "trades",
  "win_rate_pct",
  "expectancy_per_trade",
  "average_r",
  "total_charges",
  "turnover_x",
  "exposure_pct",
] as const;

export default function Backtests() {
  const runs = useData(() => api.get<BacktestRun[]>("/v1/backtests"));
  const [symbols, setSymbols] = useState("NSE:MOCKA,NSE:MOCKB,NSE:MOCKC");
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setErr(null);
    try {
      const end = new Date();
      const start = new Date(end.getTime() - days * 86_400_000);
      await api.post("/v1/backtests", {
        symbols: symbols.split(",").map((s) => s.trim()).filter(Boolean),
        start: start.toISOString(),
        end: end.toISOString(),
        data_source: "provider",
      });
      await runs.refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Backtests</h1>
      <p className="muted">
        Results on MOCK data are pipeline checks, not evidence of edge. The cost model is
        illustrative until verified against a current contract note. Never judge a strategy on
        headline return alone.
      </p>
      <ErrorBox error={err ?? runs.error} />
      <section className="card controls">
        <label>
          Symbols <input value={symbols} onChange={(e) => setSymbols(e.target.value)} size={40} />
        </label>
        <label>
          Days <input type="number" min={1} max={365} value={days} onChange={(e) => setDays(Number(e.target.value))} />
        </label>
        <button className="primary" disabled={busy} onClick={run}>
          {busy ? "Running…" : "Run backtest (provider data)"}
        </button>
      </section>
      {runs.data?.map((r) => (
        <section className="card" key={r.id}>
          <h2>
            #{r.id} {r.strategy}@{r.version} <MockTag show={r.data_is_mock} />
            {!r.cost_model.verified && <span className="tag-warn">costs unverified</span>}
          </h2>
          <p className="muted">
            {r.symbols.join(", ")} · {when(r.start)} → {when(r.end)} · run {r.run_hash.slice(0, 12)}
          </p>
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th>Full</th>
                {Object.keys(r.splits).map((s) => (
                  <th key={s}>{s}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {SHOWN.map((m) => (
                <tr key={m}>
                  <td>{m}</td>
                  <td>{String(r.metrics[m] ?? "—")}</td>
                  {Object.entries(r.splits).map(([s, v]) => (
                    <td key={s}>{String(v.metrics[m] ?? "—")}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </>
  );
}
