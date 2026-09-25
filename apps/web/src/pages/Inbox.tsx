import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOutletContext, useSearchParams } from "react-router";
import type { CallSummary, ClinicAccess, WorkflowStatus } from "../api/types";
import { useAuth } from "../auth/auth";
import { canChangeStatus, useSession } from "../auth/session";
import { CallPanel } from "../components/CallPanel";
import { DaySheet } from "../components/DaySheet";
import { config } from "../config";
import { OPEN_STATUSES } from "../lib/format";
import { coalesce } from "../lib/coalesce";
import { applyHead, applyMore, applyStatus } from "../lib/inbox";
import { followEvents } from "../lib/sse";
import "../styles/inbox.css";

type View = "todo" | "all";

const LOAD_FAILED = "Couldn't load calls. Check the connection; this page tries again as soon as live updates reconnect.";

export function Inbox() {
  const clinic = useOutletContext<ClinicAccess>();
  const { api } = useSession();
  const { token } = useAuth();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("call");
  const [calls, setCalls] = useState<CallSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("todo");
  const [live, setLive] = useState<"live" | "reconnecting">("reconnecting");
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const [detailTick, setDetailTick] = useState(0);
  const openOnly = view === "todo";
  // Bumped whenever the clinic or view changes: any response for an earlier one is dropped.
  const generation = useRef(0);
  const initialised = useRef(false);
  const known = useRef<Set<string>>(new Set());
  const detailDirty = useRef(false);
  const selected = useRef(selectedId);
  selected.current = selectedId;

  // One refresh of the first page at a time; triggers during a run collapse into one more.
  const refresh = useMemo(
    () =>
      coalesce(async () => {
        const gen = generation.current;
        try {
          const page = await api.calls(clinic.id, { limit: 50, openOnly });
          if (gen !== generation.current) return;
          setCalls((prev) => applyHead(prev, page, openOnly));
          if (!initialised.current) {
            initialised.current = true;
            setCursor(page.next_cursor);
          } else {
            const added = page.items.filter((c) => !known.current.has(c.id)).map((c) => c.id);
            if (added.length) {
              setFresh(new Set(added));
              window.setTimeout(() => setFresh(new Set()), 4000);
            }
          }
          page.items.forEach((c) => known.current.add(c.id));
          setError(null);
          setLoaded(true);
          if (detailDirty.current) {
            detailDirty.current = false;
            setDetailTick((t) => t + 1);
          }
        } catch {
          if (gen === generation.current && !initialised.current) setError(LOAD_FAILED);
        }
      }),
    [api, clinic.id, openOnly],
  );

  useEffect(() => {
    generation.current += 1;
    initialised.current = false;
    known.current = new Set();
    setCalls([]);
    setCursor(null);
    setLoaded(false);
    setError(null);
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    void followEvents({
      url: `${config.apiBase}/v1/clinics/${encodeURIComponent(clinic.id)}/events`,
      token,
      signal: controller.signal,
      onStatus: setLive,
      // (Re)connected and positioned: reload, so nothing from before or between is missed.
      onReady: () => void refresh(),
      onReset: () => void refresh(),
      onEvent: (event) => {
        let data: { call_id?: string; version?: number; workflow_status?: WorkflowStatus } = {};
        try {
          data = JSON.parse(event.data) as typeof data;
        } catch {
          /* ids only; an unreadable payload still triggers a refresh */
        }
        if (event.event === "call.workflow" && data.call_id && typeof data.version === "number") {
          const change = { call_id: data.call_id, version: data.version, workflow_status: data.workflow_status };
          setCalls((prev) => applyStatus(prev, change));
        }
        // Message events name the provider's call id, which the list doesn't carry: rare, so
        // any of them reloads an open call panel.
        if (data.call_id === selected.current || event.event.startsWith("message.")) detailDirty.current = true;
        void refresh();
      },
      onRevoked: () => {
        generation.current += 1;
        setCalls([]);
        setError("You no longer have access to this clinic.");
      },
    });
    return () => controller.abort();
  }, [clinic.id, token, refresh]);

  async function loadMore() {
    if (!cursor) return;
    const gen = generation.current;
    try {
      const page = await api.calls(clinic.id, { limit: 50, cursor, openOnly });
      if (gen !== generation.current) return;
      page.items.forEach((c) => known.current.add(c.id));
      setCalls((prev) => applyMore(prev, page));
      setCursor(page.next_cursor);
    } catch {
      if (gen === generation.current) setError("Couldn't load earlier calls. Try again.");
    }
  }

  const onChanged = useCallback((callId: string, status: WorkflowStatus, version: number) => {
    setCalls((prev) => applyStatus(prev, { call_id: callId, version, workflow_status: status }));
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
        {!loaded && !error && <p className="inbox__empty">Loading calls…</p>}
        {loaded && shown.length === 0 && !error && (
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
