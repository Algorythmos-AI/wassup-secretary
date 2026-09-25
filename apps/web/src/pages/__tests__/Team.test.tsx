import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import type { ClinicAccess, Team as TeamData } from "../../api/types";
import { Team } from "../Team";

const api = { team: vi.fn(), invite: vi.fn(), setRole: vi.fn(), removeMember: vi.fn(), revokeInvitation: vi.fn() };
let clinic: ClinicAccess = { id: "clinic-1", slug: "s", name: "Synthetic Clinic", timezone: "Australia/Sydney", role: "admin" };
vi.mock("../../auth/session", async (original) => ({
  ...(await original<typeof import("../../auth/session")>()),
  useSession: () => ({ api }),
}));
vi.mock("../../auth/auth", () => ({ useAuth: () => ({ email: "admin@example.test" }) }));
vi.mock("react-router", async (original) => ({
  ...(await original<typeof import("react-router")>()),
  useOutletContext: () => clinic,
  Navigate: ({ to }: { to: string }) => <p>redirect:{to}</p>,
}));

const team: TeamData = {
  members: [
    { staff_user_id: "u-owner", email: "owner@example.test", display_name: "Olive Owner", role: "owner", since: "2026-09-01T00:00:00Z" },
    { staff_user_id: "u-admin", email: "admin@example.test", display_name: null, role: "admin", since: "2026-09-02T00:00:00Z" },
    { staff_user_id: "u-rec", email: "rec@example.test", display_name: null, role: "receptionist", since: "2026-09-03T00:00:00Z" },
  ],
  invitations: [{ id: "inv-1", email: "new@example.test", role: "viewer", created_at: "2026-09-20T00:00:00Z" }],
};

describe("Team", () => {
  beforeEach(() => {
    Object.values(api).forEach((m) => m.mockReset());
    api.team.mockResolvedValue(team);
    clinic = { ...clinic, role: "admin" };
  });

  it("lists members and invitations, and only lets an admin touch people at or below their rank", async () => {
    render(<Team />);
    expect(await screen.findByText("owner@example.test")).toBeInTheDocument();
    expect(screen.getByText("you")).toBeInTheDocument();
    expect(screen.queryByLabelText("Role for owner@example.test")).not.toBeInTheDocument(); // above an admin
    expect(screen.getByLabelText("Role for rec@example.test")).toBeInTheDocument();
    expect(screen.getByText("new@example.test")).toBeInTheDocument();
    // An admin can't offer the owner role.
    const options = Array.from(screen.getByLabelText("Role for rec@example.test").querySelectorAll("option")).map((o) => o.value);
    expect(options).toEqual(["viewer", "receptionist", "admin"]);
  });

  it("invites by email with a role and shows the server's answer", async () => {
    api.invite.mockResolvedValue({ action: "invited", team: { ...team, invitations: [...team.invitations, { id: "inv-2", email: "x@example.test", role: "receptionist", created_at: "2026-09-25T00:00:00Z" }] } });
    render(<Team />);
    await screen.findByText("owner@example.test");
    await userEvent.type(screen.getByLabelText("Email"), "x@example.test");
    await userEvent.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() => expect(api.invite).toHaveBeenCalledWith("clinic-1", "x@example.test", "receptionist"));
    expect(await screen.findByRole("status")).toHaveTextContent("Invited x@example.test");
    expect(screen.getByText("x@example.test")).toBeInTheDocument();
  });

  it("explains a refusal instead of failing silently", async () => {
    api.setRole.mockRejectedValue(new ApiError(409, "conflict", "A clinic must keep at least one owner"));
    render(<Team />);
    await screen.findByText("owner@example.test");
    await userEvent.selectOptions(screen.getByLabelText("Role for rec@example.test"), "viewer");
    expect(await screen.findByRole("status")).toHaveTextContent("A clinic must keep at least one owner");
  });

  it("sends people without the admin role back to the inbox", () => {
    clinic = { ...clinic, role: "receptionist" };
    render(<Team />);
    expect(screen.getByText("redirect:/c/clinic-1/inbox")).toBeInTheDocument();
    expect(api.team).not.toHaveBeenCalled();
  });
});
