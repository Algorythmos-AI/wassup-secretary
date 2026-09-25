import type { CallSummary } from "../api/types";
import { buildDaySheet } from "./daysheet";

const call = (id: string, started_at: string | null): CallSummary => ({
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
});

it("groups by clinic-local day and hour, newest first, unknown times last", () => {
  const sheet = buildDaySheet(
    [
      call("a", "2026-09-24T23:05:00Z"), // 25 Sep 9:05 Sydney
      call("b", "2026-09-24T23:45:00Z"), // 25 Sep 9:45
      call("c", "2026-09-25T02:10:00Z"), // 25 Sep 12:10
      call("d", "2026-09-23T22:00:00Z"), // 24 Sep 8:00
      call("e", null),
    ],
    "Australia/Sydney",
  );
  expect(sheet.map((d) => d.date)).toEqual(["2026-09-25", "2026-09-24", null]);
  expect(sheet[0]!.hours.map((h) => [h.hour, h.calls.map((c) => c.id)])).toEqual([
    [12, ["c"]],
    [9, ["b", "a"]],
  ]);
  expect(sheet[2]!.hours[0]!.calls.map((c) => c.id)).toEqual(["e"]);
});
