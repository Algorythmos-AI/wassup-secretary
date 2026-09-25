import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AnalyticsSummary, ClinicAccess } from "../../api/types";
import { Analytics } from "../Analytics";

const api = { analytics: vi.fn() };
vi.mock("../../auth/session", async (original) => ({
  ...(await original<typeof import("../../auth/session")>()),
  useSession: () => ({ api }),
}));
const clinic: ClinicAccess = {
  id: "clinic-1",
  slug: "synthetic",
  name: "Synthetic Clinic",
  timezone: "Australia/Sydney",
  role: "receptionist",
};
vi.mock("react-router", async (original) => ({
  ...(await original<typeof import("react-router")>()),
  useOutletContext: () => clinic,
}));

function summary(days: number): AnalyticsSummary {
  const by_day = Array.from({ length: days }, (_, i) => ({
    date: `2026-09-${String(i + 1).padStart(2, "0")}`,
    calls: i % 4,
  }));
  return {
    clinic_id: clinic.id,
    timezone: clinic.timezone,
    from: by_day[0]!.date,
    to: by_day.at(-1)!.date,
    totals: {
      calls: 42,
      avg_duration_seconds: 95,
      total_duration_seconds: 3990,
      cost_usd: "5.25",
      priority: 3,
      reception_action: 20,
    },
    workflow: { pending: 4, following_up: 2, addressed: 30, no_action_needed: 6 },
    by_day,
    by_hour: Array.from({ length: 24 }, (_, hour) => ({ hour, calls: hour > 7 && hour < 18 ? 3 : 0 })),
    by_weekday: Array.from({ length: 7 }, (_, weekday) => ({ weekday, calls: weekday < 5 ? 8 : 1 })),
    sentiment: { neutral: 30, positive: 8, unknown: 4 },
    top_intents: [{ intent: "appointment_request", calls: 12 }],
  };
}

describe("Analytics", () => {
  // Block body: a function returned from beforeEach is run as teardown, and the mock is one.
  beforeEach(() => {
    api.analytics.mockReset();
  });

  it("renders the totals and every chart for a month of data", async () => {
    api.analytics.mockResolvedValue(summary(30));
    render(<Analytics />);
    expect(await screen.findByText("42")).toBeInTheDocument();
    expect(screen.getByText("still to do").previousSibling).toHaveTextContent("6");
    expect(screen.getByText("1 min 35 s")).toBeInTheDocument();
    for (const caption of ["Calls each day", "When calls come in (clinic time)", "What callers wanted", "Where follow-ups stand"]) {
      expect(screen.getAllByText(caption).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("Appointment request").length).toBeGreaterThan(0);
  });

  it("hides the voice cost from roles that don't see billing", async () => {
    api.analytics.mockResolvedValue(summary(7));
    render(<Analytics />);
    await screen.findByText("42");
    expect(screen.queryByText("voice cost")).not.toBeInTheDocument();
  });

  it("says so when analytics can't load", async () => {
    api.analytics.mockRejectedValue(new Error("down"));
    render(<Analytics />);
    expect(await screen.findByText(/couldn't load analytics/i)).toBeInTheDocument();
  });
});
