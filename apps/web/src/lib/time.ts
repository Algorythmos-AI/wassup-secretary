/** Dates and times in the clinic's own timezone (never the viewer's), en-AU formats. */

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(options)}`;
  let f = formatters.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("en-AU", { timeZone, ...options });
    formatters.set(key, f);
  }
  return f;
}

export interface LocalParts {
  date: string; // YYYY-MM-DD in the clinic's calendar
  hour: number;
  minute: number;
}

export function localParts(instant: string | Date, timeZone: string): LocalParts {
  const parts = formatter(timeZone, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(typeof instant === "string" ? new Date(instant) : instant);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    hour: Number(get("hour")) % 24,
    minute: Number(get("minute")),
  };
}

export function todayIn(timeZone: string, now: Date = new Date()): string {
  return localParts(now, timeZone).date;
}

/** Calendar arithmetic on YYYY-MM-DD strings (no timezone involved). */
export function addDays(date: string, days: number): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error(`addDays: not a YYYY-MM-DD date: ${JSON.stringify(date)}`);
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function formatTime(instant: string, timeZone: string): string {
  return formatter(timeZone, { hour: "numeric", minute: "2-digit", hour12: true })
    .format(new Date(instant))
    .replace(/\s/g, " ");
}

export function formatClock(now: Date, timeZone: string): string {
  return formatter(timeZone, { hour: "numeric", minute: "2-digit", hour12: true })
    .format(now)
    .replace(/\s/g, " ");
}

export function formatHour(hour: number): string {
  const h = hour % 12 === 0 ? 12 : hour % 12;
  return `${h} ${hour < 12 ? "am" : "pm"}`;
}

/** "Today", "Yesterday" or "Tue 22 Sept". Without ``today``, always the plain date (chart axes). */
export function formatDayLabel(date: string, today?: string): string {
  if (today) {
    if (date === today) return "Today";
    if (date === addDays(today, -1)) return "Yesterday";
  }
  // Built from parts: punctuation between them differs across ICU versions ("Tue, 22 Sept").
  const parts = formatter("UTC", { weekday: "short", day: "numeric", month: "short" }).formatToParts(
    new Date(`${date}T12:00:00Z`),
  );
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  return `${get("weekday")} ${get("day")} ${get("month")}`;
}

export function formatLongDate(date: string): string {
  return formatter("UTC", { weekday: "long", day: "numeric", month: "long", year: "numeric" }).format(
    new Date(`${date}T12:00:00Z`),
  );
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m} min ${String(s).padStart(2, "0")} s` : `${s} s`;
}
