/**
 * Typed client for the Python AI service.
 *
 * Its job is to turn backend failures into gateway-level HTTP statuses without
 * losing information. The interesting case is 502: the backend returns it when
 * an AI provider fails, which is *not* the gateway being unavailable. Mapping
 * that to 503 would tell a client the wrong thing, so it is passed through.
 */

export interface BackendClientOptions {
  baseUrl: string;
  timeoutMs: number;
  fetchImpl?: typeof fetch;
}

export class BackendError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
    readonly upstream: string,
  ) {
    super(`backend ${status} from ${upstream}: ${detail}`);
    this.name = 'BackendError';
  }
}

export class BackendUnavailable extends Error {
  constructor(readonly cause_detail: string) {
    super(`backend unreachable: ${cause_detail}`);
    this.name = 'BackendUnavailable';
  }
}

/** Map an upstream failure to the status the gateway should return. */
export function mapStatus(upstreamStatus: number): number {
  if (upstreamStatus === 401) return 401;
  if (upstreamStatus === 403) return 403;
  if (upstreamStatus === 404) return 404;
  if (upstreamStatus === 409) return 409;
  if (upstreamStatus === 422) return 422;
  if (upstreamStatus === 429) return 429;
  // The backend returns 502 when an AI provider fails: the gateway itself is
  // fine and the request was valid, so 502 is the honest answer.
  if (upstreamStatus >= 500) return 502;
  return upstreamStatus;
}

/** Extract a human-readable detail from a FastAPI error body. */
export function detailFrom(body: unknown, fallback: string): string {
  if (typeof body === 'string' && body.trim()) return body.slice(0, 500);
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail.slice(0, 500);
    if (detail !== undefined) return JSON.stringify(detail).slice(0, 500);
  }
  return fallback;
}

export class BackendClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: BackendClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, '');
    this.timeoutMs = options.timeoutMs;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  async request(
    method: string,
    path: string,
    // Optional members include `undefined` explicitly: `exactOptionalPropertyTypes`
    // is on, so passing `{ token: maybeUndefined }` is a type error otherwise.
    options: { token?: string | undefined; body?: unknown } = {},
  ): Promise<{ status: number; body: unknown }> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);

    const headers: Record<string, string> = { accept: 'application/json' };
    if (options.token) headers.authorization = `Bearer ${options.token}`;
    if (options.body !== undefined) headers['content-type'] = 'application/json';

    try {
      const init: RequestInit = {
        method,
        headers,
        signal: controller.signal,
      };
      // Assign only when there is a body: `exactOptionalPropertyTypes` rejects
      // an explicit `body: undefined`.
      if (options.body !== undefined) init.body = JSON.stringify(options.body);

      const response = await this.fetchImpl(url, init);

      const text = await response.text();
      let parsed: unknown = null;
      if (text) {
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = text;
        }
      }
      return { status: response.status, body: parsed };
    } catch (error) {
      if (error instanceof Error && error.name === 'AbortError') {
        throw new BackendUnavailable(`timeout after ${this.timeoutMs}ms`);
      }
      throw new BackendUnavailable(error instanceof Error ? error.message : String(error));
    } finally {
      clearTimeout(timer);
    }
  }

  async health(): Promise<{ status: number; body: unknown }> {
    return this.request('GET', '/api/health');
  }

  async agentMetrics(token: string): Promise<{ status: number; body: unknown }> {
    return this.request('GET', '/api/metrics/ai', { token });
  }
}