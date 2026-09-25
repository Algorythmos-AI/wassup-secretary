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

function streamOf(chunks: string[], signal: AbortSignal | null | undefined, { hang = false } = {}) {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      const abort = () => controller.error(new DOMException("aborted", "AbortError"));
      if (signal?.aborted) return abort(); // as fetch does for an already-aborted request
      for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
      signal?.addEventListener("abort", abort);
      if (!hang) controller.close();
    },
  });
}

it("resumes from the ready position even when no event arrived, and asks for a reload", async () => {
  const lastIds: (string | null)[] = [];
  const ready = vi.fn();
  const seen: string[] = [];
  const controller = new AbortController();
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    lastIds.push(new Headers(init.headers).get("Last-Event-ID"));
    if (lastIds.length === 4) controller.abort();
    return new Response(streamOf(["retry: 3000\n\nid: 77-0\nevent: ready\ndata: {}\n\n"], init.signal), { status: 200 });
  }) as typeof fetch;
  await followEvents({
    url: "/events",
    token: async () => "t",
    onEvent: (e) => seen.push(e.event),
    onReady: ready,
    signal: controller.signal,
    fetchImpl,
    sleep: async () => {},
  });
  expect(lastIds).toEqual([null, "77-0", "77-0", "77-0"]);
  expect(ready).toHaveBeenCalledTimes(3);
  expect(seen).toEqual([]); // "ready" is the client's cue to reload, not an event to handle
});

it("backs off when streams keep ending at once, and resets only after a healthy connection", async () => {
  const delays: number[] = [];
  let clock = 0;
  const controller = new AbortController();
  let n = 0;
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    n += 1;
    if (n === 6) controller.abort();
    if (n !== 4) return new Response(streamOf([], init.signal), { status: 200 });
    // The fourth connection stays up for two minutes before the server closes it.
    const body = new ReadableStream<Uint8Array>(
      {
        pull(c) {
          clock += 120_000;
          c.close();
        },
      },
      { highWaterMark: 0 }, // pulled only when the client reads, i.e. once connected
    );
    return new Response(body, { status: 200 });
  }) as typeof fetch;
  await followEvents({
    url: "/events",
    token: async () => "t",
    onEvent: () => {},
    signal: controller.signal,
    fetchImpl,
    now: () => clock,
    sleep: async (ms) => {
      delays.push(ms);
    },
  });
  // Immediate clean closes back off; after a long healthy stream the server's clean close
  // is answered promptly and the delay starts over.
  expect(delays).toEqual([1000, 2000, 4000, 250, 1000]);
});

it("drops a connection that goes silent and reconnects", async () => {
  const controller = new AbortController();
  const status: string[] = [];
  let n = 0;
  const fetchImpl = (async (_url: string, init: RequestInit) => {
    n += 1;
    if (n === 2) controller.abort();
    return new Response(streamOf([], init.signal, { hang: true }), { status: 200 });
  }) as typeof fetch;
  await followEvents({
    url: "/events",
    token: async () => "t",
    onEvent: () => {},
    onStatus: (s) => status.push(s),
    signal: controller.signal,
    fetchImpl,
    sleep: async () => {},
    idleTimeoutMs: 20,
  });
  expect(n).toBe(2);
  expect(status.slice(0, 2)).toEqual(["live", "reconnecting"]);
});
