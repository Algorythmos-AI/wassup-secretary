/**
 * Live events from core-api (Server-Sent Events over fetch, so the sign-in token travels in a
 * header, never in a URL). Reconnects with backoff, resumes from the last event id, gets a fresh
 * token when the server says the old one expired, and stops for good if access was revoked.
 */

export interface StreamEvent {
  id: string | null;
  event: string;
  data: string;
}

/** Incremental SSE parser: feed text chunks, get complete events. */
export class SseParser {
  private buffer = "";
  private id: string | null = null;
  private event = "message";
  private data: string[] = [];

  push(chunk: string): StreamEvent[] {
    this.buffer += chunk.replace(/\r\n?/g, "\n");
    const out: StreamEvent[] = [];
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (line === "") {
        if (this.data.length || this.event !== "message") {
          out.push({ id: this.id, event: this.event, data: this.data.join("\n") });
        }
        this.event = "message";
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue; // comment / keep-alive
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "id") this.id = value;
      else if (field === "event") this.event = value;
      else if (field === "data") this.data.push(value);
    }
    return out;
  }

  get lastId(): string | null {
    return this.id;
  }
}

export interface LiveOptions {
  url: string;
  token: (forceRefresh: boolean) => Promise<string>;
  onEvent: (event: StreamEvent) => void;
  /** The stream is (re)connected and positioned: reload lists now, so nothing committed while
   * disconnected (or before the first connect) can be missed. */
  onReady?: () => void;
  onReset?: () => void;
  onRevoked?: () => void;
  onStatus?: (status: "live" | "reconnecting") => void;
  signal: AbortSignal;
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  /** No bytes for this long (the server sends a keep-alive every 15 s): the connection is
   * half-open, so drop it and reconnect. */
  idleTimeoutMs?: number;
}

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const MIN_BACKOFF = 1000;
const MAX_BACKOFF = 30_000;
/** A connection must stay up this long before the retry delay goes back to the minimum, so a
 * server that accepts then drops at once is retried ever more slowly, not every second. */
const HEALTHY_AFTER_MS = 60_000;

export async function followEvents(options: LiveOptions): Promise<void> {
  const fetchImpl = options.fetchImpl ?? ((...args) => fetch(...args));
  const sleep = options.sleep ?? wait;
  const now = options.now ?? Date.now;
  const idleTimeoutMs = options.idleTimeoutMs ?? 45_000;
  let lastId: string | null = null;
  let refreshToken = false;
  let backoff = MIN_BACKOFF;
  while (!options.signal.aborted) {
    const connection = new AbortController();
    const stop = () => connection.abort();
    options.signal.addEventListener("abort", stop);
    let idle: ReturnType<typeof setTimeout> | undefined;
    const watch = () => {
      clearTimeout(idle);
      idle = setTimeout(stop, idleTimeoutMs);
    };
    let connectedAt: number | null = null;
    let failed = false;
    try {
      const headers: Record<string, string> = {
        Authorization: `Bearer ${await options.token(refreshToken)}`,
        Accept: "text/event-stream",
      };
      if (lastId) headers["Last-Event-ID"] = lastId;
      refreshToken = false;
      watch();
      const response = await fetchImpl(options.url, { headers, signal: connection.signal });
      if (response.status === 401) {
        refreshToken = true;
        throw new Error("unauthorised");
      }
      if (response.status === 403 || response.status === 404) {
        options.onRevoked?.();
        return;
      }
      if (!response.ok || !response.body) throw new Error(`http_${response.status}`);
      connectedAt = now();
      options.onStatus?.("live");
      const parser = new SseParser();
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      for (;;) {
        watch();
        const { value, done } = await reader.read();
        if (done) break;
        for (const event of parser.push(value)) {
          if (event.id) lastId = event.id;
          if (event.event === "revoked") {
            options.onRevoked?.();
            return;
          }
          if (event.event === "reauth") refreshToken = true;
          else if (event.event === "ready") options.onReady?.();
          else if (event.event === "reset") options.onReset?.();
          else options.onEvent(event);
        }
      }
    } catch {
      failed = true;
    } finally {
      clearTimeout(idle);
      options.signal.removeEventListener("abort", stop);
    }
    if (options.signal.aborted) return;
    options.onStatus?.("reconnecting");
    const healthy = connectedAt !== null && now() - connectedAt >= HEALTHY_AFTER_MS;
    if (healthy) backoff = MIN_BACKOFF;
    // A long-lived stream the server ended on purpose (token expiry, time limit): come back
    // promptly. Anything else (errors, or a stream that ended at once) backs off.
    await sleep(healthy && !failed ? 250 : backoff);
    if (!healthy || failed) backoff = Math.min(backoff * 2, MAX_BACKOFF);
  }
}
