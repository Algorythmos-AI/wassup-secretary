import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOutletContext, useSearchParams } from "react-router";
import type { CallSummary, ClinicAccess, WorkflowStatus } from "../api/types";
import { useAuth } from "../auth/auth";
import { canChangeStatus, useSession } from "../auth/session";
import { CallPanel } from "../components/CallPanel";
import { DaySheet } from "../components/DaySheet";
import { config } from "../config";
import { OPEN_STATUSES } from "../lib/format";
import { followEvents } from "../lib/sse";
import "../styles/inbox.css";

type View = "todo" | "all";

/** Whether `call` falls inside the window covered by a freshly fetched first page. */
function isRecent(call: CallSummary, page: CallSummary[]): boolean {
  const oldest = page[page.length - 1]?.started_at;
  return !!call.started_at && !!oldest && call.started_at >= oldest;
}

function merge(existing: CallSummary[], incoming: CallSummary[]): CallSummary[] {
  const byId = new Map(existing.map((c) => [c.id, c]));
  for (const c of incoming) byId.set(c.id, c);
  return [...byId.values()].sort((a, b) => ((a.started_at ?? "") < (b.started_at ?? "") ? 1 : -1));
}

export function Inbox() {
  const clinic = useOutletContext<ClinicAccess>();
  const { api } = useSession();
  const { token } = useAuth();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("call");
  const [calls, setCalls] = useState<CallSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("todo");
  const [live, setLive] = useState<"live" | "reconnecting">("reconnecting");
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [detailTick, setDetailTick] = useState(0);
  const known = useRef<Set<string>>(new Set());

  const refreshHead = useCallback(
    async (markNew: boolean) => {
      const page = await api.calls(clinic.id, { limit: 50, openOnly: view === "todo" });
      // In the to-do view, a call someone just finished drops out of the list.
      setCalls((prev) =>
        merge(
          view === "todo" ? prev.filter((c) => page.items.some((i) => i.id === c.id) || !isRecent(c, page.items)) : prev,
          page.items,
        ),
      );
      if (markNew) {
        const added = page.items.filter((c) => !known.current.has(c.id)).map((c) => c.id);
        if (added.length) {
          setFresh(new Set(added));
          window.setTimeout(() => setFresh(new Set()), 4000);
        }
      }
      page.items.forEach((c) => known.current.add(c.id));
      return page;
    },
    [api, clinic.id, view],
  );

  useEffect(() => {
    let cancelled = false;
    setCalls([]);
    known.current = new Set();
    setLoading(true);
    refreshHead(false)
      .then((page) => {
        if (!cancelled) {
          setCursor(page.next_cursor);
          setError(null);
        }
      })
      .catch(() => !cancelled && setError("Couldn't load calls. Check the connection; this page retries when live updates reconnect."))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [refreshHead]);

  useEffect(() => {
    const controller = new AbortController();
    void followEvents({
      url: `${config.apiBase}/v1/clinics/${clinic.id}/events`,
      token,
      signal: controller.signal,
      onStatus: setLive,
      onEvent: () => {
        void refreshHead(true).catch(() => undefined);
        setDetailTick((t) => t + 1);
      },
      onReset: () => void refreshHead(false).catch(() => undefined),
      onRevoked: () => setError("You no longer have access to this clinic."),
    });
    return () => controller.abort();
  }, [clinic.id, token, refreshHead]);

  async function loadMore() {
    if (!cursor) return;
    const page = await api.calls(clinic.id, { limit: 50, cursor, openOnly: view === "todo" });
    page.items.forEach((c) => known.current.add(c.id));
    setCalls((prev) => merge(prev, page.items));
    setCursor(page.next_cursor);
  }

  const onChanged = useCallback((callId: string, status: WorkflowStatus, version: number) => {
    setCalls((prev) => prev.map((c) => (c.id === callId ? { ...c, workflow_status: status, version } : c)));
  }, []);

  const shown = useMemo(
    () => (view === "todo" ? calls.filter((c) => OPEN_STATUSES.has(c.workflow_status)) : calls),
    [calls, view],
  );
  const todoLabel = view === "todo" ? `${shown.length}${cursor ? "+" : ""}` : null;
  const select = (id: string | null) => setParams(id ? { call: id } : {}, { replace: true });

  return (
    <div className={`inbox${selectedId ? " inbox--with-panel" : ""}`}>
      <div className="inbox__list">
        <header className="page-header">
          <h1>Inbox</h1>
          <div className="tabs" role="tablist" aria-label="Which calls">
            <button role="tab" type="button" aria-selected={view === "todo"} onClick={() => setView("todo")}>
              To do {todoLabel && <span className="tabs__count">{todoLabel}</span>}
            </button>
            <button role="tab" type="button" aria-selected={view === "all"} onClick={() => setView("all")}>
              All calls
            </button>
          </div>
          <span className={`live live--${live}`}>{live === "live" ? "Live" : "Reconnecting…"}</span>
        </header>
        {error && <p className="notice notice--error">{error}</p>}
        {loading && <p className="inbox__empty">Loading calls…</p>}
        {!loading && shown.length === 0 && !error && (
          <p className="inbox__empty">
            {view === "todo" ? "Nothing to follow up. New calls appear here as they finish." : "No calls yet."}
          </p>
        )}
        <DaySheet calls={shown} timeZone={clinic.timezone} selectedId={selectedId} freshIds={fresh} onSelect={select} />
        {cursor && (
          <button type="button" className="button inbox__more" onClick={() => void loadMore()}>
            Load earlier calls
          </button>
        )}
      </div>
      {selectedId && (
        <CallPanel
          clinicId={clinic.id}
          callId={selectedId}
          timeZone={clinic.timezone}
          canEdit={canChangeStatus(clinic.role)}
          refreshSignal={detailTick}
          onChanged={onChanged}
          onClose={() => select(null)}
        />
      )}
    </div>
  );
}
