"use client";

import { useEffect } from "react";

import { api, CONTROLS_CHANGED, type Controls, type Mode } from "@/lib/api";

import { useData } from "./ui";

/** Always-visible banner: trading mode, PAPER labelling and the live-trading lock. */
export function ModeBanner() {
  const { data, error } = useData(() => api.get<Mode>("/v1/system/mode"));
  const controls = useData(() => api.get<Controls>("/v1/risk/controls?history=0"));
  const refreshControls = controls.refresh;
  useEffect(() => {
    // The kill-switch state must never be stale on screen: poll, and refresh on local changes.
    const id = setInterval(() => void refreshControls(), 10_000);
    const onChange = () => void refreshControls();
    window.addEventListener(CONTROLS_CHANGED, onChange);
    return () => {
      clearInterval(id);
      window.removeEventListener(CONTROLS_CHANGED, onChange);
    };
  }, [refreshControls]);
  const kill = controls.data?.kill_switch_active;
  if (error) {
    return <div className="banner banner-down">API unreachable — showing no data. ({error})</div>;
  }
  if (!data) return <div className="banner">Loading mode…</div>;
  const paper = data.trading_mode === "PAPER";
  return (
    <>
    {kill && (
      <div className="banner banner-kill" role="alert" data-kill="active">
        KILL SWITCH ACTIVE — no new risk can be opened. Reason: {controls.data?.kill_switch?.reason}
      </div>
    )}
    <div className={`banner ${paper ? "banner-paper" : "banner-research"}`} data-mode={data.trading_mode}>
      <strong>{paper ? "PAPER TRADING — simulated orders and fills only" : "RESEARCH ONLY — no orders"}</strong>
      <span>mode {data.trading_mode}</span>
      <span>env {data.environment}</span>
      <span>live trading {data.live_trading_available ? "AVAILABLE" : "locked"}</span>
      <span>IST {new Date(data.server_time_exchange).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })}</span>
    </div>
    </>
  );
}
