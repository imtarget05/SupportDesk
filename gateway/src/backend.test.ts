import { describe, expect, it, vi } from 'vitest';
import { BackendClient, BackendUnavailable, detailFrom, mapStatus } from './backend.js';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function clientReturning(response: Response | Error, baseUrl = 'http://backend.test') {
  const impl = vi.fn(async () => {
    if (response instanceof Error) throw response;
    return response.clone();
  });
  const fetchImpl = impl as unknown as typeof fetch;
  return {
    client: new BackendClient({ baseUrl, timeoutMs: 1000, fetchImpl }),
    spy: impl,
  };
}

describe('mapStatus', () => {
  it('passes client errors through unchanged', () => {
    expect(mapStatus(401)).toBe(401);
    expect(mapStatus(403)).toBe(403);
    expect(mapStatus(404)).toBe(404);
    expect(mapStatus(409)).toBe(409);
    expect(mapStatus(422)).toBe(422);
    expect(mapStatus(429)).toBe(429);
  });

  it('keeps a backend 502 as 502', () => {
    // The backend answers 502 when an AI provider fails. The gateway is fine
    // and the request was valid, so a 503 here would be a lie.
    expect(mapStatus(502)).toBe(502);
  });

  it('maps other server errors to 502', () => {
    expect(mapStatus(500)).toBe(502);
    expect(mapStatus(503)).toBe(502);
  });
});

describe('detailFrom', () => {
  it('reads a FastAPI detail string', () => {
    expect(detailFrom({ detail: 'Ticket not found' }, 'fallback')).toBe('Ticket not found');
  });

  it('handles a validation array', () => {
    const body = { detail: [{ loc: ['body', 'subject'], msg: 'required' }] };
    expect(detailFrom(body, 'fallback')).toContain('required');
  });

  it('falls back for an unknown shape', () => {
    expect(detailFrom({}, 'fallback')).toBe('fallback');
  });

  it('truncates a very long detail', () => {
    expect(detailFrom('x'.repeat(5000), 'fallback')).toHaveLength(500);
  });
});

describe('BackendClient', () => {
  it('prefixes the configured base URL', async () => {
    const { client, spy: fetchImpl } = clientReturning(jsonResponse(200, { ok: true }));
    await client.request('GET', '/api/health');
    expect(fetchImpl).toHaveBeenCalledWith(
      'http://backend.test/api/health',
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('strips a trailing slash from the base URL', async () => {
    const { client, spy: fetchImpl } = clientReturning(
      jsonResponse(200, {}),
      'http://backend.test/',
    );
    await client.request('GET', '/api/health');
    expect((fetchImpl.mock.calls[0] as unknown[])[0]).toBe('http://backend.test/api/health');
  });

  it('parses a JSON body', async () => {
    const { client } = clientReturning(jsonResponse(200, { documents_loaded: 3 }));
    const result = await client.request('GET', '/api/health');
    expect(result.body).toEqual({ documents_loaded: 3 });
  });

  it('passes a non-JSON body through as text', async () => {
    const { client } = clientReturning(new Response('plain text', { status: 200 }));
    const result = await client.request('GET', '/api/health');
    expect(result.body).toBe('plain text');
  });

  it('sends the bearer token', async () => {
    const { client, spy: fetchImpl } = clientReturning(jsonResponse(200, {}));
    await client.request('GET', '/api/tickets', { token: 'abc' });
    const init = (fetchImpl.mock.calls[0] as unknown[])[1] as RequestInit;
    expect((init.headers as Record<string, string>).authorization).toBe('Bearer abc');
  });

  it('omits the auth header when there is no token', async () => {
    const { client, spy: fetchImpl } = clientReturning(jsonResponse(200, {}));
    await client.request('GET', '/api/health');
    const init = (fetchImpl.mock.calls[0] as unknown[])[1] as RequestInit;
    expect((init.headers as Record<string, string>).authorization).toBeUndefined();
  });

  it('serializes a JSON body', async () => {
    const { client, spy: fetchImpl } = clientReturning(jsonResponse(201, {}));
    await client.request('POST', '/api/tickets', { body: { subject: 'hi' } });
    const init = (fetchImpl.mock.calls[0] as unknown[])[1] as RequestInit;
    expect(init.body).toBe('{"subject":"hi"}');
  });

  it('reports an unreachable backend as BackendUnavailable', async () => {
    const { client } = clientReturning(new TypeError('fetch failed'));
    await expect(client.request('GET', '/api/health')).rejects.toBeInstanceOf(BackendUnavailable);
  });

  it('reports a timeout as BackendUnavailable', async () => {
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    const { client } = clientReturning(abort);
    const error = await client.request('GET', '/api/health').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(BackendUnavailable);
    expect((error as BackendUnavailable).message).toMatch(/unreachable/);
  });
});