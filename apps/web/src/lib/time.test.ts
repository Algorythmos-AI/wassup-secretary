import { addDays, formatDayLabel, formatDuration, formatHour, formatTime, localParts, todayIn } from "./time";

describe("clinic-local time", () => {
  it("places the same instant on the clinic's own calendar", () => {
    // 23:30 UTC on 24 Sep = 09:30 Sydney (AEST) and 09:30 Brisbane on 25 Sep.
    expect(localParts("2026-09-24T23:30:00Z", "Australia/Sydney")).toEqual({ date: "2026-09-25", hour: 9, minute: 30 });
    expect(localParts("2026-09-24T23:30:00Z", "Australia/Brisbane")).toEqual({ date: "2026-09-25", hour: 9, minute: 30 });
  });

  it("follows daylight saving in Sydney but not in Brisbane (starts 4 Oct 2026)", () => {
    const afterChange = "2026-10-04T23:30:00Z";
    expect(localParts(afterChange, "Australia/Sydney").hour).toBe(10); // AEDT, UTC+11
    expect(localParts(afterChange, "Australia/Brisbane").hour).toBe(9); // AEST all year
  });

  it("formats en-AU", () => {
    expect(formatTime("2026-09-24T23:05:00Z", "Australia/Sydney")).toBe("9:05 am");
    expect(formatHour(0)).toBe("12 am");
    expect(formatHour(12)).toBe("12 pm");
    expect(formatHour(15)).toBe("3 pm");
    expect(formatDuration(65)).toBe("1 min 05 s");
    expect(formatDuration(9)).toBe("9 s");
  });

  it("labels days relative to the clinic's today", () => {
    expect(todayIn("Australia/Sydney", new Date("2026-09-24T15:00:00Z"))).toBe("2026-09-25");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
    expect(formatDayLabel("2026-09-25", "2026-09-25")).toBe("Today");
    expect(formatDayLabel("2026-09-24", "2026-09-25")).toBe("Yesterday");
    // Without "today" (chart axes) it is always the plain date, never a crash.
    expect(formatDayLabel("2026-09-25")).toBe("Fri 25 Sept");
    expect(() => addDays("", 1)).toThrow(/not a YYYY-MM-DD date/);
    expect(formatDayLabel("2026-09-22", "2026-09-25")).toBe("Tue 22 Sept");
  });
});
