"use client";

import { useState } from "react";

import { useMe } from "@/components/Session";
import { ErrorBox, useData, when } from "@/components/ui";
import { api, type Enrollment, type UserRowOut } from "@/lib/api";

function PasswordForm() {
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [code, setCode] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await api.post("/v1/auth/password", { current_password: cur, new_password: nw, code });
      setMsg("Password changed. Your other sessions were signed out.");
      setCur("");
      setNw("");
      setCode("");
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }
  return (
    <form className="form" onSubmit={submit}>
      <label>
        Current password
        <input type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} required />
      </label>
      <label>
        New password (12+ characters)
        <input type="password" autoComplete="new-password" minLength={12} value={nw} onChange={(e) => setNw(e.target.value)} required />
      </label>
      <label>
        Authenticator code
        <input inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={(e) => setCode(e.target.value)} required />
      </label>
      {err && <div className="error">{err}</div>}
      {msg && <div className="note">{msg}</div>}
      <div className="row">
        <button className="primary">Change password</button>
      </div>
    </form>
  );
}

function Users() {
  const users = useData(() => api.get<UserRowOut[]>("/v1/users"));
  const [username, setUsername] = useState("");
  const [role, setRole] = useState("viewer");
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState<Enrollment | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function act(fn: () => Promise<Enrollment | unknown>) {
    setErr(null);
    try {
      const r = await fn();
      if (r && typeof r === "object" && "totp_secret" in r) setShown(r as Enrollment);
      await users.refresh();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }

  return (
    <section className="card">
      <h2>Users (owner only)</h2>
      <ErrorBox error={err ?? users.error} />
      {shown && (
        <div className="secret">
          <strong>Authenticator secret for {shown.username}</strong> — shown once. Enter it in the authenticator app now
          (manual entry, time-based):
          <br />
          {shown.totp_secret}
          <br />
          <span className="muted">{shown.provisioning_uri}</span>
          <br />
          <button onClick={() => setShown(null)}>I have saved it</button>
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th>Created</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {users.data?.map((u) => (
            <tr key={u.username}>
              <td>{u.username}</td>
              <td>{u.role}</td>
              <td>
                {when(u.created_at)} <span className="muted">by {u.created_by}</span>
              </td>
              <td>
                {u.disabled ? "disabled" : u.locked_until && new Date(u.locked_until) > new Date() ? "locked" : "active"}
              </td>
              <td>
                <button onClick={() => act(() => api.post(`/v1/users/${u.username}/${u.disabled ? "enable" : "disable"}`))}>
                  {u.disabled ? "Enable" : "Disable"}
                </button>{" "}
                <button onClick={() => act(() => api.post<Enrollment>(`/v1/users/${u.username}/reset-mfa`))}>Reset authenticator</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Add a user</h3>
      <form
        className="form"
        onSubmit={(e) => {
          e.preventDefault();
          void act(() => api.post<Enrollment>("/v1/users", { username, role, password }));
        }}
      >
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value.toLowerCase())} required />
        </label>
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="viewer">viewer — read only</option>
            <option value="approver">approver — can decide approvals and activate the kill switch</option>
            <option value="owner">owner — full control</option>
          </select>
        </label>
        <label>
          Initial password (12+ characters; ask them to change it)
          <input type="password" autoComplete="new-password" minLength={12} value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <div className="row">
          <button className="primary">Create user</button>
        </div>
      </form>
    </section>
  );
}

export default function Account() {
  const me = useMe();
  return (
    <>
      <h1>Account</h1>
      <section className="card">
        <h2>
          {me?.username ?? "—"} · {me?.role ?? ""}
        </h2>
        {me && !me.auth_enabled ? (
          <p className="muted">Sign-in is turned off in this local demo; there is no account to manage.</p>
        ) : (
          <PasswordForm />
        )}
      </section>
      {me?.role === "owner" && me.auth_enabled && <Users />}
    </>
  );
}
