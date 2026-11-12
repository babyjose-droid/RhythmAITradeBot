"use client";

import Link from "next/link";
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";

import { api, setReauthHandler, type Me, type NotificationSummary } from "@/lib/api";

const SessionCtx = createContext<Me | null>(null);
export const useMe = () => useContext(SessionCtx);

function ReauthDialog({ onDone, mfa }: { onDone: (ok: boolean) => void; mfa: boolean }) {
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.post("/v1/auth/reauth", { password, code });
      onDone(true);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="reauth-title">
      <form className="modal" onSubmit={submit}>
        <h2 id="reauth-title">Confirm it is you</h2>
        <p className="muted">This action changes risk controls or users. Enter your password{mfa ? " and a fresh authenticator code" : ""}.</p>
        <label>
          Password
          <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {mfa && (
          <label>
            6-digit code
            <input inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} required />
          </label>
        )}
        {err && <div className="error">{err}</div>}
        <div className="row">
          <button type="button" onClick={() => onDone(false)}>
            Cancel
          </button>
          <button className="primary" disabled={busy}>
            Confirm
          </button>
        </div>
      </form>
    </div>
  );
}

export function SessionBar({ children }: { children?: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [summary, setSummary] = useState<NotificationSummary | null>(null);
  const [asking, setAsking] = useState(false);
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  const load = useCallback(async () => {
    try {
      setMe(await api.get<Me>("/v1/auth/me"));
      setSummary(await api.get<NotificationSummary>("/v1/notifications/summary"));
    } catch {
      setMe(null);
    }
  }, []);

  useEffect(() => {
    if (window.location.pathname.startsWith("/login")) return;
    void load();
    const t = setInterval(() => void load(), 30_000);
    setReauthHandler(
      () =>
        new Promise<boolean>((resolve) => {
          resolver.current = resolve;
          setAsking(true);
        }),
    );
    return () => {
      clearInterval(t);
      setReauthHandler(null);
    };
  }, [load]);

  async function logout() {
    await api.post("/v1/auth/logout").catch(() => null);
    window.location.assign("/login");
  }

  const crit = summary?.unread.critical ?? 0;
  return (
    <SessionCtx.Provider value={me}>
      {me && (
        <div className="session">
          <Link href="/notifications" className={`bell ${crit ? "bell-critical" : ""}`} title="Notifications">
            🔔 {summary?.unread_total ?? 0}
            {crit > 0 && <span className="bell-crit"> ({crit} critical)</span>}
          </Link>
          <span className="who">
            {me.username} · <strong>{me.role}</strong>
            {!me.auth_enabled && <span className="tag-warn"> AUTH OFF (local demo)</span>}
          </span>
          {me.auth_enabled && (
            <>
              <Link href="/account">Account</Link>
              <button className="linkish" onClick={logout}>
                Sign out
              </button>
            </>
          )}
        </div>
      )}
      {children}
      {asking && (
        <ReauthDialog
          mfa={me?.mfa_required !== false}
          onDone={(ok) => {
            setAsking(false);
            resolver.current?.(ok);
            resolver.current = null;
            void load();
          }}
        />
      )}
    </SessionCtx.Provider>
  );
}
