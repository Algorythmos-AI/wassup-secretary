import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { SessionProvider, useSession } from "../session";

const me = vi.fn();
const auth = { token: async () => "t", status: "signed-in" }; // stable, as the real provider's
vi.mock("../auth", () => ({ useAuth: () => auth }));
vi.mock("../../api/client", async (original) => {
  const actual = await original<typeof import("../../api/client")>();
  return {
    ...actual,
    Api: class {
      me = me;
    },
  };
});

function Probe() {
  const { me: who, error } = useSession();
  return <p>{who ? `signed in as ${who.email}` : `error: ${error ?? "none"}`}</p>;
}

beforeEach(() => {
  vi.useFakeTimers();
  me.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

it("keeps retrying while the API can't be reached, then recovers without a reload", async () => {
  me.mockRejectedValueOnce(new TypeError("Failed to fetch"))
    .mockRejectedValueOnce(new ApiError(503, "unavailable", "deploying"))
    .mockResolvedValue({ email: "tv@clinic.example.test", clinics: [] });
  render(
    <SessionProvider>
      <Probe />
    </SessionProvider>,
  );
  await act(async () => {});
  expect(screen.getByText("error: unavailable")).toBeInTheDocument();
  await act(async () => vi.advanceTimersByTimeAsync(2000));
  await act(async () => vi.advanceTimersByTimeAsync(4000));
  expect(screen.getByText("signed in as tv@clinic.example.test")).toBeInTheDocument();
  expect(me).toHaveBeenCalledTimes(3);
});

it("stops and explains when the sign-in itself is rejected", async () => {
  me.mockRejectedValue(new ApiError(401, "unauthenticated", "no"));
  render(
    <SessionProvider>
      <Probe />
    </SessionProvider>,
  );
  await act(async () => {});
  await act(async () => vi.advanceTimersByTimeAsync(120_000));
  expect(screen.getByText("error: not_accepted")).toBeInTheDocument();
  expect(me).toHaveBeenCalledOnce();
});
