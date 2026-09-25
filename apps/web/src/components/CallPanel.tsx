import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import type { CallDetail, WorkflowStatus } from "../api/types";
import { useSession } from "../auth/session";
import { STATUS_LABEL, formatPhone, humanizeIntent } from "../lib/format";
import { formatDuration, formatTime, localParts, formatDayLabel, todayIn } from "../lib/time";
import { StatusPill } from "./StatusPill";

interface Props {
  clinicId: string;
  callId: string;
  timeZone: string;
  canEdit: boolean;
  refreshSignal: number;
  onChanged: (callId: string, status: WorkflowStatus, version: number) => void;
  onClose: () => void;
}

const NEXT: Record<WorkflowStatus, { status: WorkflowStatus; label: string; primary?: boolean }[]> = {
  pending: [
    { status: "following_up", label: "Start follow-up", primary: true },
    { status: "addressed", label: "Mark done" },
    { status: "no_action_needed", label: "No action needed" },
  ],
  following_up: [
    { status: "addressed", label: "Mark done", primary: true },
    { status: "no_action_needed", label: "No action needed" },
    { status: "pending", label: "Back to to do" },
  ],
  addressed: [{ status: "pending", label: "Reopen" }],
  no_action_needed: [{ status: "pending", label: "Reopen" }],
};

/** "1:20 pm" today, "Yesterday 4:05 pm" or "Tue 22 Sept 9:10 am" otherwise. */
function whenLabel(iso: string, timeZone: string, today: string): string {
  const { date } = localParts(iso, timeZone);
  const time = formatTime(iso, timeZone);
  return date === today ? time : `${formatDayLabel(date, today)} ${time}`;
}

export function CallPanel({ clinicId, callId, timeZone, canEdit, refreshSignal, onChanged, onClose }: Props) {
  const { api } = useSession();
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setDetail(await api.call(clinicId, callId));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 404 ? "This call isn't available." : "Couldn't load this call. Try again.");
    }
  }, [api, clinicId, callId]);

  useEffect(() => {
    setDetail(null);
    setNote("");
    setMessage(null);
    void load();
  }, [load]);

  useEffect(() => {
    if (refreshSignal) void load();
  }, [refreshSignal, load]);

  async function change(status: WorkflowStatus) {
    if (!detail) return;
    setSaving(true);
    setMessage(null);
    try {
      const result = await api.setStatus(clinicId, callId, {
        status,
        note: note.trim() || undefined,
        version: detail.call.version,
        key: crypto.randomUUID(),
      });
      setNote("");
      setMessage(`Marked ${STATUS_LABEL[result.workflow_status].toLowerCase()}.`);
      onChanged(callId, result.workflow_status, result.version);
      await load();
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) {
        setMessage("Someone else updated this call. It's been refreshed, so check it and try again.");
        await load();
      } else if (e instanceof ApiError && e.status === 403) {
        setMessage("Your role can view calls but not change them.");
      } else {
        setMessage("That change didn't save. Try again.");
      }
    } finally {
      setSaving(false);
    }
  }

  const call = detail?.call;
  const today = todayIn(timeZone);
  return (
    <aside className="panel" aria-label="Call details">
      <div className="panel__bar">
        <button type="button" className="panel__close" onClick={onClose}>
          Close
        </button>
      </div>
      {error && <p className="notice notice--error">{error}</p>}
      {!call && !error && <p className="panel__loading">Loading call…</p>}
      {call && detail && (
        <>
          <header className="panel__header">
            <h2>{humanizeIntent(call.intent)}</h2>
            <p className="panel__meta">
              {call.started_at
                ? `${formatDayLabel(localParts(call.started_at, timeZone).date, today)} at ${formatTime(call.started_at, timeZone)}`
                : "Time unknown"}
              {call.duration_seconds != null && `, ${formatDuration(call.duration_seconds)} long`}
            </p>
            <p className="panel__caller">{formatPhone(call.from_number)}</p>
            <StatusPill status={call.workflow_status} />
            {call.has_urgent_message && <span className="call__flag call__flag--urgent">Urgent message</span>}
            {call.is_priority && <span className="call__flag">Priority</span>}
          </header>

          {canEdit && (
            <div className="panel__actions">
              <label className="panel__note">
                <span>Note for the team (optional)</span>
                <textarea value={note} maxLength={2000} rows={2} onChange={(e) => setNote(e.target.value)} />
              </label>
              <div className="panel__buttons">
                {NEXT[call.workflow_status].map((action) => (
                  <button
                    key={action.status}
                    type="button"
                    className={`button${action.primary ? " button--primary" : ""}`}
                    disabled={saving}
                    onClick={() => void change(action.status)}
                  >
                    {action.label}
                  </button>
                ))}
              </div>
            </div>
          )}
          {message && (
            <p className="panel__message" role="status">
              {message}
            </p>
          )}

          {call.summary && (
            <section className="panel__section">
              <h3>Summary</h3>
              <p>{call.summary}</p>
            </section>
          )}

          {detail.messages.length > 0 && (
            <section className="panel__section">
              <h3>Messages taken</h3>
              <ul className="panel__messages">
                {detail.messages.map((m, i) => (
                  <li key={i} className={m.urgent ? "panel__urgent" : undefined}>
                    {m.urgent && <strong>Urgent. </strong>}
                    <span className="panel__plain">{m.detail}</span>
                    {m.callback_number && <span className="panel__callback">Call back on {formatPhone(m.callback_number)}</span>}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {call.transcript && (
            <section className="panel__section">
              <details>
                <summary>Transcript</summary>
                <p className="panel__plain panel__transcript">{call.transcript}</p>
              </details>
            </section>
          )}

          {detail.interactions.length > 0 && (
            <section className="panel__section">
              <h3>History</h3>
              <ol className="panel__history">
                {detail.interactions.map((h, i) => (
                  <li key={i}>
                    <span className="panel__when">{whenLabel(h.created_at, timeZone, today)}</span>{" "}
                    {h.status_to ? `Marked ${STATUS_LABEL[h.status_to as WorkflowStatus]?.toLowerCase() ?? h.status_to}` : h.action_type}
                    {h.note && <span className="panel__plain">: {h.note}</span>}
                  </li>
                ))}
              </ol>
            </section>
          )}
        </>
      )}
    </aside>
  );
}
