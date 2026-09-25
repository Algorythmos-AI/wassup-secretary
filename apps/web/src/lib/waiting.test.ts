import { describe, expect, it } from "vitest";
import type { CallSummary } from "../api/types";
import { waited, waitingOrder } from "./waiting";

const now = new Date("2026-09-25T03:00:00Z");
const call = (id: string, started_at: string, flags: Partial<CallSummary> = {}): CallSummary => ({
  id,
  started_at,
  from_number: null,
  duration_seconds: null,
  summary: null,
  intent: null,
  workflow_status: "pending",
  is_priority: false,
  is_reception_action: false,
  priority_level: null,
  action_label: null,
  has_urgent_message: false,
  version: 1,
  ...flags,
});

describe("office TV waiting board", () => {
  it("says how long someone has waited in words a room can read", () => {
    expect(waited("2026-09-25T02:48:00Z", now)).toBe("12 min");
    expect(waited("2026-09-24T23:55:00Z", now)).toBe("3 h 05 min");
    expect(waited("2026-09-24T02:00:00Z", now)).toBe("1 day");
    expect(waited("2026-09-14T03:00:00Z", now)).toBe("11 days");
    expect(waited("2026-09-25T03:05:00Z", now)).toBe("0 min"); // clock skew: never negative
  });

  it("puts urgent messages first, then priority, then the longest wait", () => {
    const calls = [
      call("old", "2026-09-10T01:00:00Z"),
      call("priority", "2026-09-24T01:00:00Z", { is_priority: true }),
      call("urgent-new", "2026-09-25T02:00:00Z", { has_urgent_message: true }),
      call("older", "2026-09-09T01:00:00Z"),
      call("urgent-old", "2026-09-25T01:00:00Z", { has_urgent_message: true, is_priority: true }),
    ];
    expect([...calls].sort(waitingOrder).map((c) => c.id)).toEqual([
      "urgent-old",
      "urgent-new",
      "priority",
      "older",
      "old",
    ]);
  });
});
