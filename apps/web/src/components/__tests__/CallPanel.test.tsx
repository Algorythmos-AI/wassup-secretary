import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { CallDetail } from "../../api/types";
import { CallPanel } from "../CallPanel";

const api = { call: vi.fn(), setStatus: vi.fn() };
vi.mock("../../auth/session", () => ({ useSession: () => ({ api }) }));

function detail(overrides: Partial<CallDetail["call"]> = {}): CallDetail {
  return {
    call: {
      id: "call-1",
      provider_call_id: "p1",
      direction: "inbound",
      started_at: "2026-09-22T00:05:00Z",
      ended_at: "2026-09-22T00:07:00Z",
      from_number: "+61412345678",
      to_number: "+61238211140",
      duration_seconds: 125,
      summary: "Caller asked for a callback about their results.",
      intent: "callback_request",
      workflow_status: "pending",
      is_priority: false,
      is_reception_action: true,
      has_urgent_message: true,
      version: 4,
      cost_usd: "0.12",
      disconnection_reason: null,
      transcript: "Agent: Hello <b>there</b> <script>alert(1)</script>",
      sentiment: "neutral",
      local_date: "2026-09-22",
      local_hour: 10,
      analyzed_at: "2026-09-22T00:08:00Z",
      ...overrides,
    },
    messages: [
      { category: "post_op", detail: "<img src=x onerror=alert(1)>", callback_number: null, urgent: true, created_at: "2026-09-22T00:06:00Z" },
      { category: "urgent", detail: "Routine question about parking.", callback_number: null, urgent: false, created_at: "2026-09-22T00:06:30Z" },
    ],
    interactions: [],
  };
}

function renderPanel(props: Partial<Parameters<typeof CallPanel>[0]> = {}) {
  const onChanged = vi.fn();
  render(
    <CallPanel
      clinicId="clinic-1"
      callId="call-1"
      timeZone="Australia/Sydney"
      canEdit
      refreshSignal={0}
      onChanged={onChanged}
      onClose={() => undefined}
      {...props}
    />,
  );
  return { onChanged };
}

describe("CallPanel", () => {
  beforeEach(() => {
    api.call.mockReset();
    api.setStatus.mockReset();
  });

  it("sends the version the user saw, then reports the change", async () => {
    api.call.mockResolvedValueOnce(detail()).mockResolvedValueOnce(detail({ workflow_status: "following_up", version: 5 }));
    api.setStatus.mockResolvedValue({ call_id: "call-1", workflow_status: "following_up", version: 5 });
    const { onChanged } = renderPanel();

    await userEvent.type(await screen.findByLabelText(/note for the team/i), "Rang back, left voicemail");
    await userEvent.click(screen.getByRole("button", { name: "Start follow-up" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith("call-1", "following_up", 5));
    const [, , change] = api.setStatus.mock.calls[0]!;
    expect(change).toMatchObject({ status: "following_up", version: 4, note: "Rang back, left voicemail" });
    expect(change.key).toMatch(/^[0-9a-f-]{36}$/);
    expect(await screen.findByRole("status")).toHaveTextContent("Marked following up.");
  });

  it("on a conflict, says so, reloads the call and keeps the note", async () => {
    api.call.mockResolvedValueOnce(detail()).mockResolvedValueOnce(detail({ workflow_status: "addressed", version: 5 }));
    api.setStatus.mockRejectedValue(new ApiError(412, "version_conflict", "Stale"));
    const { onChanged } = renderPanel();

    await userEvent.type(await screen.findByLabelText(/note for the team/i), "My note");
    await userEvent.click(screen.getByRole("button", { name: "Mark done" }));

    expect(await screen.findByRole("status")).toHaveTextContent(/someone else updated this call/i);
    await waitFor(() => expect(api.call).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "Reopen" })).toBeInTheDocument();
    expect(screen.getByLabelText(/note for the team/i)).toHaveValue("My note");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("shows no actions to someone who can only view", async () => {
    api.call.mockResolvedValue(detail());
    renderPanel({ canEdit: false });
    expect(await screen.findByText(/callback about their results/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start follow-up" })).not.toBeInTheDocument();
  });

  it("shows what callers said as plain text, never as markup", async () => {
    api.call.mockResolvedValue(detail());
    const { container } = render(
      <CallPanel
        clinicId="clinic-1"
        callId="call-1"
        timeZone="Australia/Sydney"
        canEdit={false}
        refreshSignal={0}
        onChanged={() => undefined}
        onClose={() => undefined}
      />,
    );
    expect(await screen.findByText(/<img src=x/)).toBeInTheDocument();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
    expect(container.querySelector("script, img, b")).toBeNull();
  });

  it("marks urgency from the urgent flag, not from the category's wording", async () => {
    api.call.mockResolvedValue(detail());
    renderPanel({ canEdit: false });
    const urgent = (await screen.findByText(/<img src=x/)).closest("li")!;
    const routine = screen.getByText("Routine question about parking.").closest("li")!;
    expect(urgent).toHaveTextContent(/^Urgent\./);
    expect(routine).not.toHaveTextContent(/Urgent/);
    expect(screen.getByText("Urgent message")).toBeInTheDocument();
  });

  it("says when a call isn't available", async () => {
    api.call.mockRejectedValue(new ApiError(404, "not_found", "Not found"));
    renderPanel();
    expect(await screen.findByText("This call isn't available.")).toBeInTheDocument();
  });
});
