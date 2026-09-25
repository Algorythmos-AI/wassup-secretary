import { SseParser, followEvents, type StreamEvent } from "./sse";

it("parses events split across chunks and ignores keep-alives", () => {
  const p = new SseParser();
  expect(p.push("retry: 3000\n\n: keep-alive\n\nid: 10-4\nevent: call.anal")).toEqual([]);
  expect(p.push('yzed\ndata: {"call_id":"x"}\n\n')).toEqual([
    { id: "10-4", event: "call.analyzed", data: '{"call_id":"x"}' },
  ]);
  expect(p.lastId).toBe("10-4");
});

function stream(text: string, status = 200): Response {
  return new Response(text, { status, headers: { "content-type": "text/event-stream" } });
}

it("resumes from the last id, refreshes the token on reauth, and stops when revoked", async () => {
  const calls: { auth: string; lastId: string | null }[] = [];
  const responses = [
    stream("id: 5-1\nevent: message.urgent\ndata: {}\n\nevent: reauth\ndata: {}\n\n"),
    stream("event: revoked\ndata: {}\n\n"),
  ];
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    const headers = init.headers as Record<string, string>;
    calls.push({ auth: headers.Authorization!, lastId: headers["Last-Event-ID"] ?? null });
    return responses.shift()!;
  }) as unknown as typeof fetch;
  const seen: StreamEvent[] = [];
  const revoked = vi.fn();
  let tokenCalls = 0;
  await followEvents({
    url: "https://api.test/v1/clinics/c/events",
    token: async (force) => `t${++tokenCalls}${force ? "-fresh" : ""}`,
    onEvent: (e) => seen.push(e),
    onRevoked: revoked,
    signal: new AbortController().signal,
    fetchImpl,
    sleep: async () => {},
  });
  expect(seen.map((e) => e.event)).toEqual(["message.urgent"]);
  expect(calls).toEqual([
    { auth: "Bearer t1", lastId: null },
    { auth: "Bearer t2-fresh", lastId: "5-1" },
  ]);
  expect(revoked).toHaveBeenCalledOnce();
});

it("backs off and retries after a network error", async () => {
  let attempts = 0;
  const delays: number[] = [];
  const controller = new AbortController();
  const fetchImpl = (async () => {
    attempts += 1;
    if (attempts < 3) throw new TypeError("network down");
    controller.abort();
    return stream("");
  }) as unknown as typeof fetch;
  await followEvents({
    url: "u",
    token: async () => "t",
    onEvent: () => {},
    signal: controller.signal,
    fetchImpl,
    sleep: async (ms) => {
      delays.push(ms);
    },
  });
  expect(delays.slice(0, 2)).toEqual([1000, 2000]);
});
