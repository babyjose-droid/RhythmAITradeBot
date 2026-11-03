"use client";

import { useState } from "react";

import { ErrorBox, useData, when } from "@/components/ui";
import { api, type Notification, type NotificationSummary } from "@/lib/api";
import { severityTone } from "@/lib/status";

export default function Notifications() {
  const [unread, setUnread] = useState(false);
  const list = useData(() => api.get<Notification[]>(`/v1/notifications?limit=200${unread ? "&unread=true" : ""}`), [unread]);
  const summary = useData(() => api.get<NotificationSummary>("/v1/notifications/summary"));
  const [err, setErr] = useState<string | null>(null);

  async function read(ids: number[] | null) {
    try {
      await api.post("/v1/notifications/read", { ids });
      await Promise.all([list.refresh(), summary.refresh()]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  const s = summary.data;
  return (
    <>
      <h1>Notifications</h1>
      <ErrorBox error={err ?? list.error} />
      <section className="card controls">
        <span>
          Unread: {s?.unread_total ?? "—"} ({s?.unread.critical ?? 0} critical, {s?.unread.warning ?? 0} warning)
        </span>
        <label>
          <input type="checkbox" checked={unread} onChange={(e) => setUnread(e.target.checked)} /> unread only
        </label>
        <button onClick={() => read(null)} disabled={!s?.unread_total}>
          Mark all read
        </button>
        <span className="muted">
          Webhook: {s?.webhook_configured ? `on (severity ≥ ${s.webhook_min_severity})` : "not configured (in-app only)"}
        </span>
      </section>
      <section className="card">
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Severity</th>
              <th>What</th>
              <th>Webhook</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {list.data?.map((n) => (
              <tr key={n.id} className={`notif-${n.severity} ${n.read_at ? "" : "notif-unread"}`}>
                <td>{when(n.created_at)}</td>
                <td>
                  <span className={`tone tone-${severityTone(n.severity)}`}>{n.severity}</span>
                </td>
                <td>
                  {n.link ? <a href={n.link}>{n.title}</a> : n.title}
                  {n.body && <div className="muted">{n.body}</div>}
                </td>
                <td className="muted">
                  {n.webhook.status.toLowerCase().replace("_", " ")}
                  {n.webhook.error ? ` (${n.webhook.error})` : ""}
                </td>
                <td>
                  {n.read_at ? (
                    <span className="muted">read by {n.read_by}</span>
                  ) : (
                    <button onClick={() => read([n.id])}>Mark read</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {list.data?.length === 0 && <p className="muted">No notifications.</p>}
      </section>
    </>
  );
}
