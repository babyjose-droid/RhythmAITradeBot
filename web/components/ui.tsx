"use client";

import { useCallback, useEffect, useState } from "react";

import { decisionBadge, freshnessTone, orderBadge, type BadgeSpec } from "@/lib/status";

export function Badge({ spec }: { spec: BadgeSpec }) {
  return (
    <span className={`badge badge-${spec.kind}`} title={spec.title} data-kind={spec.kind}>
      {spec.label}
    </span>
  );
}

export const OrderBadge = ({ display }: { display: string }) => <Badge spec={orderBadge(display)} />;
export const DecisionBadge = ({ decision }: { decision: string }) => <Badge spec={decisionBadge(decision)} />;

export function FreshnessPill({ status }: { status: string }) {
  const tone = freshnessTone(status);
  return <span className={`pill pill-${tone}`}>{tone === "stale" ? `⚠ ${status}` : status}</span>;
}

export function MockTag({ show }: { show: boolean }) {
  return show ? <span className="tag-mock" title="Synthetic data, not real market prices">MOCK DATA</span> : null;
}

export function ErrorBox({ error }: { error: string | null }) {
  return error ? <div className="error" role="alert">{error}</div> : null;
}

/** Fetch helper with loading/error state and a manual refresh. */
export function useData<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(loader, deps);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setData(await load());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [load]);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  return { data, error, loading, refresh };
}

export function money(v: string | null | undefined): string {
  if (v == null) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}` : v;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
}
