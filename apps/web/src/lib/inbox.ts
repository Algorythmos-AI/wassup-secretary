/** The inbox list as pure functions, so every way a list can change is testable on its own. */
import type { CallPage, CallSummary, WorkflowStatus } from "../api/types";

/** The server's order: newest first, calls without a start time last, then by id (descending). */
export function serverOrder(a: CallSummary, b: CallSummary): number {
  if (a.started_at !== b.started_at) {
    if (a.started_at === null) return 1;
    if (b.started_at === null) return -1;
    return Date.parse(b.started_at) - Date.parse(a.started_at);
  }
  return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
}

/**
 * Fold a freshly fetched first page into the list. The page is the truth for every call it
 * covers: in the to-do view (open calls only) a call inside the page's window but missing from
 * it has been dealt with, so it goes; if the page is the whole list (no next cursor) it replaces
 * the list outright. Older calls from pages loaded earlier are kept.
 */
export function applyHead(prev: readonly CallSummary[], page: CallPage, openOnly: boolean): CallSummary[] {
  if (openOnly && page.next_cursor === null) return [...page.items].sort(serverOrder);
  const last = page.items[page.items.length - 1];
  const inPage = new Set(page.items.map((c) => c.id));
  const kept = prev.filter((c) => {
    if (inPage.has(c.id)) return false; // replaced by the fresh copy
    if (!openOnly || !last) return true;
    return serverOrder(c, last) > 0; // older than the page covers: can't tell, keep
  });
  return [...kept, ...page.items].sort(serverOrder);
}

/** Add an older page ("Load earlier calls"). */
export function applyMore(prev: readonly CallSummary[], page: CallPage): CallSummary[] {
  const byId = new Map(prev.map((c) => [c.id, c]));
  for (const c of page.items) byId.set(c.id, c);
  return [...byId.values()].sort(serverOrder);
}

/** A status change announced by another screen; stale announcements are ignored. */
export function applyStatus(
  prev: readonly CallSummary[],
  change: { call_id: string; version: number; workflow_status?: WorkflowStatus },
): CallSummary[] {
  return prev.map((c) =>
    c.id === change.call_id && change.version > c.version && change.workflow_status
      ? { ...c, workflow_status: change.workflow_status, version: change.version }
      : c,
  );
}
