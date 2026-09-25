/** Group calls into a day sheet: clinic-local days (newest first), then hours, like an
 * appointment book read backwards from now. Calls without a start time go last. */
import type { CallSummary } from "../api/types";
import { localParts } from "./time";

export interface SheetHour {
  hour: number;
  calls: CallSummary[];
}

export interface SheetDay {
  date: string | null; // null: time unknown
  hours: SheetHour[];
}

export function buildDaySheet(calls: readonly CallSummary[], timeZone: string): SheetDay[] {
  const days = new Map<string, Map<number, CallSummary[]>>();
  const unknown: CallSummary[] = [];
  for (const call of calls) {
    if (!call.started_at) {
      unknown.push(call);
      continue;
    }
    const { date, hour } = localParts(call.started_at, timeZone);
    const hours = days.get(date) ?? new Map<number, CallSummary[]>();
    days.set(date, hours);
    hours.set(hour, [...(hours.get(hour) ?? []), call]);
  }
  const sheet: SheetDay[] = [...days.entries()]
    .sort(([a], [b]) => (a < b ? 1 : -1))
    .map(([date, hours]) => ({
      date,
      hours: [...hours.entries()]
        .sort(([a], [b]) => b - a)
        .map(([hour, list]) => ({
          hour,
          calls: [...list].sort((x, y) => (x.started_at! < y.started_at! ? 1 : -1)),
        })),
    }));
  if (unknown.length) sheet.push({ date: null, hours: [{ hour: -1, calls: unknown }] });
  return sheet;
}
