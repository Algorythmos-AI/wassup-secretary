import type { WorkflowStatus } from "../api/types";

export const STATUS_LABEL: Record<WorkflowStatus, string> = {
  pending: "To do",
  following_up: "Following up",
  addressed: "Done",
  no_action_needed: "No action needed",
};

export const OPEN_STATUSES: ReadonlySet<WorkflowStatus> = new Set(["pending", "following_up"]);

/** "callback_request" → "Callback request". Unknown or empty intents read as "Call". */
export function humanizeIntent(intent: string | null | undefined): string {
  if (!intent) return "Call";
  const words = intent.replace(/[_-]+/g, " ").trim().toLowerCase();
  return words ? words[0]!.toUpperCase() + words.slice(1) : "Call";
}

/** Australian grouping: +61 412 345 678 (mobile), +61 2 3821 1140 (landline). */
/** Australian numbers the way reception dials them: 0412 345 678, (02) 3821 1140. */
export function formatPhone(e164: string | null | undefined): string {
  if (!e164) return "Number withheld";
  const m = /^\+61(\d{9})$/.exec(e164);
  if (!m) return e164;
  const n = m[1]!;
  if (n.startsWith("4")) return `0${n.slice(0, 3)} ${n.slice(3, 6)} ${n.slice(6)}`;
  if (/^[2378]/.test(n)) return `(0${n[0]}) ${n.slice(1, 5)} ${n.slice(5)}`;
  return `0${n}`;
}

/** For shared screens: only the last three digits. */
export function maskPhone(e164: string | null | undefined): string {
  if (!e164) return "Number withheld";
  return `ends ${e164.slice(-3)}`;
}
