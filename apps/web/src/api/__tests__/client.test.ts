import { describe, expect, it, vi } from "vitest";
import { Api, ApiError } from "../client";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("Api", () => {
  it("sends the ID token and retries once with a fresh token on 401", async () => {
    const token = vi.fn(async (force?: boolean) => (force ? "fresh" : "stale"));
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(json({ code: "unauthenticated" }, 401))
      .mockResolvedValueOnce(json({ email: "a@b.c", clinics: [] }));
    const api = new Api(token, "https://api.test", fetchImpl);

    await expect(api.me()).resolves.toEqual({ email: "a@b.c", clinics: [] });
    const auth = fetchImpl.mock.calls.map(([, init]) => new Headers(init?.headers).get("Authorization"));
    expect(auth).toEqual(["Bearer stale", "Bearer fresh"]);
  });

  it("gives up after one retry", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ code: "unauthenticated" }, 401));
    const api = new Api(async () => "t", "https://api.test", fetchImpl);
    await expect(api.me()).rejects.toMatchObject({ status: 401, code: "unauthenticated" });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("sends the version seen and an idempotency key with a status change", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ call_id: "c", workflow_status: "addressed", version: 3 }));
    const api = new Api(async () => "t", "https://api.test", fetchImpl);

    await api.setStatus("clinic", "call", { status: "addressed", note: "", version: 2, key: "same-click-retried" });
    const [url, init] = fetchImpl.mock.calls[0]!;
    const headers = new Headers(init?.headers);
    expect(url).toBe("https://api.test/v1/clinics/clinic/calls/call/workflow");
    expect(init?.method).toBe("POST");
    expect(headers.get("If-Match")).toBe("2");
    expect(headers.get("Idempotency-Key")).toBe("same-click-retried");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init?.body))).toEqual({ status: "addressed", note: null });
  });

  it("turns a problem document into an ApiError", async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValue(json({ code: "version_conflict", title: "Precondition Failed", detail: "Stale" }, 412));
    const api = new Api(async () => "t", "https://api.test", fetchImpl);
    const error = await api.call("clinic", "call").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 412, code: "version_conflict", message: "Stale" });
  });

  it("copes with an error body that isn't JSON", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502 }));
    const api = new Api(async () => "t", "https://api.test", fetchImpl);
    await expect(api.me()).rejects.toMatchObject({ status: 502, code: "http_502" });
  });

  it("asks the server for open calls only when told to", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(json({ items: [], next_cursor: null }));
    const api = new Api(async () => "t", "https://api.test", fetchImpl);
    await api.calls("clinic", { openOnly: true, cursor: "abc" });
    const url = new URL(String(fetchImpl.mock.calls[0]![0]));
    expect(url.searchParams.get("open_only")).toBe("true");
    expect(url.searchParams.get("cursor")).toBe("abc");
  });
});
