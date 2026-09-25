/**
 * Typed client for core-api. Every request carries the signed-in user's ID token; errors come
 * back as RFC 9457 problem documents and surface as ApiError with the status and a short code.
 */
import { config } from "../config";
import type {
  AnalyticsSummary,
  CallDetail,
  CallPage,
  Me,
  UsageReport,
  WorkflowResult,
  WorkflowStatus,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export type TokenSource = (forceRefresh?: boolean) => Promise<string>;

export class Api {
  constructor(
    private readonly token: TokenSource,
    private readonly base = config.apiBase,
    private readonly fetchImpl: typeof fetch = (...args) => fetch(...args),
  ) {}

  private async send(path: string, init: RequestInit, forceRefresh: boolean): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${await this.token(forceRefresh)}`);
    if (init.body) headers.set("Content-Type", "application/json");
    return this.fetchImpl(`${this.base}${path}`, { ...init, headers });
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    let response = await this.send(path, init, false);
    // An expired token between refreshes: get a fresh one and try once more.
    if (response.status === 401) response = await this.send(path, init, true);
    if (!response.ok) {
      let code = `http_${response.status}`;
      let detail = response.statusText;
      try {
        const problem = (await response.json()) as { code?: string; title?: string; detail?: string };
        code = problem.code ?? code;
        detail = problem.detail ?? problem.title ?? detail;
      } catch {
        /* not a problem document */
      }
      throw new ApiError(response.status, code, detail);
    }
    return (await response.json()) as T;
  }

  me(): Promise<Me> {
    return this.request("/v1/me");
  }

  calls(
    clinicId: string,
    options: { limit?: number; cursor?: string | null; openOnly?: boolean } = {},
  ): Promise<CallPage> {
    const params = new URLSearchParams({ limit: String(options.limit ?? 50) });
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.openOnly) params.set("open_only", "true");
    return this.request(`/v1/clinics/${clinicId}/calls?${params}`);
  }

  call(clinicId: string, callId: string): Promise<CallDetail> {
    return this.request(`/v1/clinics/${clinicId}/calls/${callId}`);
  }

  /** Change a call's status. `version` is the one the user saw (412 if someone else changed it
   * since); `key` makes a retried click safe (the same key never applies twice). */
  setStatus(
    clinicId: string,
    callId: string,
    change: { status: WorkflowStatus; note?: string; version: number; key: string },
  ): Promise<WorkflowResult> {
    return this.request(`/v1/clinics/${clinicId}/calls/${callId}/workflow`, {
      method: "POST",
      headers: { "Idempotency-Key": change.key, "If-Match": String(change.version) },
      body: JSON.stringify({ status: change.status, note: change.note || null }),
    });
  }

  analytics(clinicId: string, range: { from?: string; to?: string } = {}): Promise<AnalyticsSummary> {
    const params = new URLSearchParams(Object.entries(range).filter(([, v]) => v) as [string, string][]);
    return this.request(`/v1/clinics/${clinicId}/analytics/summary?${params}`);
  }

  usage(clinicId: string, range: { from?: string; to?: string } = {}): Promise<UsageReport> {
    const params = new URLSearchParams(Object.entries(range).filter(([, v]) => v) as [string, string][]);
    return this.request(`/v1/clinics/${clinicId}/usage?${params}`);
  }
}
