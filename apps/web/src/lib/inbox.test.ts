import { describe, expect, it } from "vitest";
import type { CallSummary } from "../api/types";
import { applyHead, applyMore, applyStatus, serverOrder } from "./inbox";

const call = (id: string, minute: number | null, flags: Partial<CallSummary> = {}): CallSummary => ({
  id,
  started_at: minute === null ? null : new Date(Date.UTC(2026, 8, 25, 0, minute)).toISOString(),
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
const ids = (calls: CallSummary[]) => calls.map((c) => c.id);

describe("inbox list", () => {
  it("orders like the server: newest first, undated last", () => {
    expect(ids([call("a", 1), call("u", null), call("c", 3), call("b", 2)].sort(serverOrder))).toEqual([
      "c",
      "b",
      "a",
      "u",
    ]);
  });

  it("drops the oldest open call once a colleague closes it (whole list fits one page)", () => {
    const prev = [call("c", 3), call("b", 2), call("a", 1)];
    const next = applyHead(prev, { items: [call("c", 3), call("b", 2)], next_cursor: null }, true);
    expect(ids(next)).toEqual(["c", "b"]);
  });

  it("empties the to-do list when the last open call is closed", () => {
    expect(applyHead([call("a", 1)], { items: [], next_cursor: null }, true)).toEqual([]);
  });

  it("keeps older loaded calls the fresh page can't speak for", () => {
    const prev = [call("e", 5), call("d", 4), call("b", 2), call("a", 1)]; // a, b from "load earlier"
    const next = applyHead(prev, { items: [call("f", 6), call("d", 4)], next_cursor: "more" }, true);
    // e is inside the page's window (between f and d) yet missing: closed. a and b are older
    // than anything the page covers: kept.
    expect(ids(next)).toEqual(["f", "d", "b", "a"]);
  });

  it("never drops calls from the all-calls view", () => {
    const prev = [call("b", 2), call("a", 1)];
    const next = applyHead(prev, { items: [call("c", 3), call("b", 2, { workflow_status: "addressed" })], next_cursor: "x" }, false);
    expect(ids(next)).toEqual(["c", "b", "a"]);
    expect(next[1]!.workflow_status).toBe("addressed");
  });

  it("merges an older page without duplicates", () => {
    expect(ids(applyMore([call("c", 3), call("b", 2)], { items: [call("b", 2), call("a", 1)], next_cursor: null }))).toEqual([
      "c",
      "b",
      "a",
    ]);
  });

  it("applies another screen's status change to that exact call, ignoring stale ones", () => {
    const prev = [call("a", 1, { version: 3 })];
    expect(applyStatus(prev, { call_id: "a", version: 4, workflow_status: "addressed" })[0]!.workflow_status).toBe("addressed");
    expect(applyStatus(prev, { call_id: "a", version: 2, workflow_status: "addressed" })[0]!.workflow_status).toBe("pending");
  });
});
