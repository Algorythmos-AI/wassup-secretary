/** The office TV's "waiting for a call back" board. */
import type { CallSummary } from "../api/types";

/** How long a caller has waited: "12 min", "3 h 05 min", "4 days". */
export function waited(startedAt: string, now: Date): string {
  const minutes = Math.max(0, Math.floor((now.getTime() - Date.parse(startedAt)) / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ${String(minutes % 60).padStart(2, "0")} min`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "1 day" : `${days} days`;
}

/** Urgent messages first, then priority calls, then whoever has waited longest. */
export function waitingOrder(a: CallSummary, b: CallSummary): number {
  const rank = (c: CallSummary) => (c.has_urgent_message ? 0 : c.is_priority ? 1 : 2);
  return rank(a) - rank(b) || Date.parse(a.started_at!) - Date.parse(b.started_at!);
}
