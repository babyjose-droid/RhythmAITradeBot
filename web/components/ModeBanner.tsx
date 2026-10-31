"use client";

import { api, type Mode } from "@/lib/api";

import { useData } from "./ui";

/** Always-visible banner: trading mode, PAPER labelling and the live-trading lock. */
export function ModeBanner() {
  const { data, error } = useData(() => api.get<Mode>("/v1/system/mode"));
  if (error) {
    return <div className="banner banner-down">API unreachable — showing no data. ({error})</div>;
  }
  if (!data) return <div className="banner">Loading mode…</div>;
  const paper = data.trading_mode === "PAPER";
  return (
    <div className={`banner ${paper ? "banner-paper" : "banner-research"}`} data-mode={data.trading_mode}>
      <strong>{paper ? "PAPER TRADING — simulated orders and fills only" : "RESEARCH ONLY — no orders"}</strong>
      <span>mode {data.trading_mode}</span>
      <span>env {data.environment}</span>
      <span>live trading {data.live_trading_available ? "AVAILABLE" : "locked"}</span>
      <span>IST {new Date(data.server_time_exchange).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}</span>
    </div>
  );
}
