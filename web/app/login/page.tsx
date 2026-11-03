"use client";

import { useEffect, useState } from "react";

import { api, type AuthStatus } from "@/lib/api";

export default function Login() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get<AuthStatus>("/v1/auth/status").then(setStatus, () => setStatus(null));
  }, []);

  function next(): string {
    const n = new URLSearchParams(window.location.search).get("next") ?? "/";
    return n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/login") ? n : "/";
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.post("/v1/auth/login", { username, password, code });
      window.location.assign(next());
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
      setCode("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <section className="card" style={{ width: "min(420px, 100%)" }}>
        <h1>Sign in</h1>
        {status && !status.enabled && (
          <p className="note">
            Sign-in is turned off in this local demo. <a href="/">Continue to the dashboard</a>.
          </p>
        )}
        {status?.enabled && !status.has_users && (
          <div className="error">
            <strong>No users yet.</strong> Create the owner account on the machine running the API:
            <pre className="mono">docker compose exec api rhythm-cli user-create --username owner --role owner</pre>
            It prints an authenticator secret once — add it to Google Authenticator, Authy or 1Password.
          </div>
        )}
        <form className="form" onSubmit={submit}>
          <label>
            Username
            <input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
          </label>
          <label>
            Password
            <input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </label>
          <label>
            Authenticator code
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              maxLength={6}
              value={code}
              onChange={(e) => setCode(e.target.value)}
              required
            />
          </label>
          {err && <div className="error" role="alert">{err}</div>}
          <div className="row">
            <button className="primary" disabled={busy}>
              Sign in
            </button>
          </div>
        </form>
        <p className="muted">PAPER trading only. After 5 failed attempts the account locks for 15 minutes.</p>
      </section>
    </div>
  );
}
