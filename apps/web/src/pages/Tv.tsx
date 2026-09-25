/** Office TV: today at wall scale. Masked numbers and no summaries (patients may see it). */
import { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useParams } from "react-router";
import type { AnalyticsSummary, CallSummary } from "../api/types";
import { useAuth } from "../auth/auth";
import { useSession } from "../auth/session";
import { config } from "../config";
import { buildDaySheet } from "../lib/daysheet";
import { OPEN_STATUSES, STATUS_LABEL, humanizeIntent, maskPhone } from "../lib/format";
import { coalesce } from "../lib/coalesce";
import { followEvents } from "../lib/sse";
import { useReloadOnNewVersion } from "../lib/version";
import { waited, waitingOrder } from "../lib/waiting";
import { formatClock, formatHour, formatLongDate, formatTime, todayIn } from "../lib/time";
import "../styles/tv.css";

const URGENT_BANNER_MS = 90_000;
const FALLBACK_REFRESH_MS = 5 * 60_000;

export function Tv() {
  const { clinicId } = useParams();
  const { clinic, api, me } = useSession();
  const { token } = useAuth();
  const current = clinic(clinicId);
  const [now, setNow] = useState(() => new Date());
  const [calls, setCalls] = useState<CallSummary[]>([]);
  const [open, setOpen] = useState<CallSummary[]>([]);
  const [moreOpen, setMoreOpen] = useState(false);
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [live, setLive] = useState<"live" | "reconnecting">("reconnecting");
  const [revoked, setRevoked] = useState(false);
  const [urgentAt, setUrgentAt] = useState<number | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const generation = useRef(0);
  useReloadOnNewVersion();

  const timeZone = current?.timezone ?? "Australia/Sydney";
  const today = todayIn(timeZone, now);

  // One refresh at a time; a burst of events collapses into one more.
  const refresh = useMemo(
    () =>
      coalesce(async () => {
        if (!current) return;
        const gen = generation.current;
        const day = todayIn(current.timezone);
        // The waiting board needs both ends of the open list: the longest waits (oldest first)
        // and anything that just came in (newest first), merged and ranked on the client.
        const [page, newestOpen, oldestOpen, stats] = await Promise.all([
          api.calls(current.id, { limit: 100 }),
          api.calls(current.id, { limit: 100, openOnly: true }),
          api.calls(current.id, { limit: 100, openOnly: true, order: "oldest" }),
          api.analytics(current.id, { from: day, to: day }),
        ]);
        if (gen !== generation.current) return;
        const merged = new Map([...oldestOpen.items, ...newestOpen.items].map((c) => [c.id, c]));
        setCalls(page.items);
        setOpen([...merged.values()]);
        setMoreOpen(newestOpen.next_cursor !== null && oldestOpen.next_cursor !== null && merged.size >= 200);
        setSummary(stats);
        setUpdatedAt(new Date());
      }),
    [api, current],
  );

  useEffect(() => {
    generation.current += 1;
    setRevoked(false);
    const clock = window.setInterval(() => setNow(new Date()), 15_000);
    const fallback = window.setInterval(() => void refresh(), FALLBACK_REFRESH_MS);
    void refresh();
    return () => {
      window.clearInterval(clock);
      window.clearInterval(fallback);
    };
  }, [refresh]);

  useEffect(() => {
    if (!current) return;
    const controller = new AbortController();
    void followEvents({
      url: `${config.apiBase}/v1/clinics/${encodeURIComponent(current.id)}/events`,
      token,
      signal: controller.signal,
      onStatus: setLive,
      onReady: () => void refresh(),
      onReset: () => void refresh(),
      onEvent: (event) => {
        if (event.event === "message.urgent") setUrgentAt(Date.now());
        void refresh();
      },
      onRevoked: () => {
        // Access removed: clear the board rather than leave patients' calls on a wall screen.
        generation.current += 1;
        setRevoked(true);
        setCalls([]);
        setOpen([]);
        setSummary(null);
      },
    });
    return () => controller.abort();
  }, [current, token, refresh]);

  if (me && !current) return <Navigate to="/" replace />;
  if (!current) return null;
  if (revoked) {
    return (
      <div className="tv tv--message">
        <p>This screen no longer has access to {current.name}. Sign in again to show the board.</p>
      </div>
    );
  }

  const todays = calls.filter((c) => c.started_at && todayIn(timeZone, new Date(c.started_at)) === today);
  const waiting = open
    .filter((c) => c.started_at)
    .sort(waitingOrder);
  const sheet = buildDaySheet(todays, timeZone)[0];
  const showUrgent = urgentAt !== null && now.getTime() - urgentAt < URGENT_BANNER_MS;
  const todo = (summary?.workflow.pending ?? 0) + (summary?.workflow.following_up ?? 0);

  return (
    <div className="tv">
      {showUrgent && (
        <div className="tv__urgent" role="alert">
          Urgent message just taken. Check the inbox now.
        </div>
      )}
      <header className="tv__top">
        <div>
          <h1 className="tv__clinic">{current.name}</h1>
          <p className="tv__date">{formatLongDate(today)}</p>
        </div>
        <div className="tv__clock" aria-label="Clinic time">
          {formatClock(now, timeZone)}
        </div>
      </header>

      <section className="tv__stats" aria-label="Today">
        <div>
          <span className="tv__num">{summary?.totals.calls ?? "–"}</span>
          <span>calls today</span>
        </div>
        <div className={todo > 0 ? "tv__stat--todo" : undefined}>
          <span className="tv__num">{summary ? todo : "–"}</span>
          <span>still to do</span>
        </div>
        <div className={(summary?.totals.priority ?? 0) > 0 ? "tv__stat--priority" : undefined}>
          <span className="tv__num">{summary?.totals.priority ?? "–"}</span>
          <span>priority</span>
        </div>
      </section>

      <div className="tv__body">
        <section className="tv__sheet" aria-label="Today's calls">
          <h2>Today</h2>
          {!sheet && <p className="tv__quiet">No calls yet today.</p>}
          {sheet?.hours.map((slot) => (
            <div className="tv__hour" key={slot.hour}>
              <div className="tv__gutter">{formatHour(slot.hour)}</div>
              <ol>
                {slot.calls.map((c) => (
                  <li key={c.id} className={OPEN_STATUSES.has(c.workflow_status) ? "tv__call tv__call--open" : "tv__call"}>
                    <span className="tv__time">{formatTime(c.started_at!, timeZone)}</span>
                    <span className="tv__what">
                      {humanizeIntent(c.intent)}
                      {c.has_urgent_message && <span className="tv__flag">Urgent</span>}
                      {c.is_priority && !c.has_urgent_message && <span className="tv__flag">Priority</span>}
                    </span>
                    <span className="tv__status">{STATUS_LABEL[c.workflow_status]}</span>
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </section>

        <section className="tv__waiting" aria-label="Waiting for a call back">
          <h2>Waiting for a call back</h2>
          {waiting.length === 0 && <p className="tv__quiet">Everyone has been called back.</p>}
          <ol>
            {waiting.slice(0, 12).map((c) => (
              <li key={c.id} className={c.is_priority || c.has_urgent_message ? "tv__wait tv__wait--priority" : "tv__wait"}>
                <span className="tv__waited">{waited(c.started_at!, now)}</span>
                <span className="tv__what">{humanizeIntent(c.intent)}</span>
                <span className="tv__caller">{maskPhone(c.from_number)}</span>
              </li>
            ))}
          </ol>
          {(waiting.length > 12 || moreOpen) && (
            <p className="tv__quiet">
              and {moreOpen ? "more" : waiting.length - 12} in the inbox
            </p>
          )}
        </section>
      </div>

      <footer className="tv__footer">
        <span className={`tv__live tv__live--${live}`}>{live === "live" ? "Live" : "Reconnecting…"}</span>
        {updatedAt && <span>Updated {formatTime(updatedAt.toISOString(), timeZone)}</span>}
      </footer>
    </div>
  );
}
