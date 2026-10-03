import { SignJWT } from 'jose';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BackendClient } from './backend.js';
import { loadConfig } from './config.js';
import { buildServer } from './server.js';

const SECRET = 'g'.repeat(32);

function config() {
  return loadConfig({
    JWT_SECRET: SECRET,
    BACKEND_URL: 'http://backend.test',
    LOG_LEVEL: 'silent',
  } as NodeJS.ProcessEnv);
}

async function token(payload: Record<string, unknown> = { sub: '1', role: 'agent' }) {
  return new SignJWT(payload)
    .setProtectedHeader({ alg: 'HS256' })
    .setExpirationTime('5m')
    .sign(new TextEncoder().encode(SECRET));
}

function backendReturning(handler: (url: string, init?: RequestInit) => Response) {
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  return new BackendClient({
    baseUrl: 'http://backend.test',
    timeoutMs: 1000,
    fetchImpl: fetchImpl as unknown as typeof fetch,
  });
}

async function appWith(handler: (url: string, init?: RequestInit) => Response) {
  const app = await buildServer({ config: config(), backend: backendReturning(handler) });
  await app.ready();
  return app;
}

const OK_JSON = () =>
  new Response(JSON.stringify({ documents_loaded: 2 }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

const UNAVAILABLE = () => {
  throw new TypeError('fetch failed');
};

describe('gateway local endpoints', () => {
  let app: Awaited<ReturnType<typeof appWith>>;

  beforeEach(async () => {
    app = await appWith(OK_JSON);
  });

  it('serves /healthz without auth', async () => {
    const res = await app.inject({ method: 'GET', url: '/healthz' });
    expect(res.statusCode).toBe(200);
    expect(res.json()).toEqual({ status: 'ok' });
  });

  it('reports ready when the backend is healthy', async () => {
    const res = await app.inject({ method: 'GET', url: '/readyz' });
    expect(res.statusCode).toBe(200);
    expect(res.json().status).toBe('ready');
  });

  it('reports 503 when the backend is down', async () => {
    const failing = await appWith(UNAVAILABLE);
    const res = await failing.inject({ method: 'GET', url: '/readyz' });
    expect(res.statusCode).toBe(503);
    expect(res.json().status).toBe('unavailable');
    await failing.close();
  });
});

describe('gateway authentication', () => {
  it('rejects a request with no token', async () => {
    const app = await appWith(OK_JSON);
    const res = await app.inject({ method: 'GET', url: '/api/tickets' });
    expect(res.statusCode).toBe(401);
    expect(res.json().detail).toMatch(/Missing bearer token/);
    await app.close();
  });

  it('rejects a token signed with the wrong secret', async () => {
    const app = await appWith(OK_JSON);
    const bad = await new SignJWT({ sub: '1', role: 'agent' })
      .setProtectedHeader({ alg: 'HS256' })
      .setExpirationTime('5m')
      .sign(new TextEncoder().encode('x'.repeat(32)));
    const res = await app.inject({
      method: 'GET',
      url: '/api/tickets',
      headers: { authorization: `Bearer ${bad}` },
    });
    expect(res.statusCode).toBe(401);
    await app.close();
  });

  it('rejects an expired token', async () => {
    const app = await appWith(OK_JSON);
    const expired = await new SignJWT({ sub: '1', role: 'agent' })
      .setProtectedHeader({ alg: 'HS256' })
      .setExpirationTime('-1m')
      .sign(new TextEncoder().encode(SECRET));
    const res = await app.inject({
      method: 'GET',
      url: '/api/tickets',
      headers: { authorization: `Bearer ${expired}` },
    });
    expect(res.statusCode).toBe(401);
    await app.close();
  });

  it('lets the public ticket endpoint through unauthenticated', async () => {
    // The contact form must work before signup; the backend rate-limits it.
    let called = false;
    const app = await appWith(() => {
      called = true;
      return new Response(JSON.stringify({ id: 1 }), { status: 201 });
    });
    const res = await app.inject({ method: 'POST', url: '/api/tickets', payload: {} });
    expect(res.statusCode).toBe(201);
    expect(called).toBe(true);
    await app.close();
  });

  it('lets login and register through unauthenticated', async () => {
    const app = await appWith(
      () => new Response(JSON.stringify({ ok: true }), { status: 200 }),
    );
    expect(
      (await app.inject({ method: 'POST', url: '/api/auth/login', payload: {} })).statusCode,
    ).toBe(200);
    expect(
      (await app.inject({ method: 'POST', url: '/api/auth/register', payload: {} })).statusCode,
    ).toBe(200);
    await app.close();
  });
});

describe('gateway proxying', () => {
  it('forwards an authenticated request with the token', async () => {
    let seenAuth: string | undefined;
    const app = await appWith((_url, init) => {
      seenAuth = (init?.headers as Record<string, string> | undefined)?.authorization;
      return OK_JSON();
    });
    const jwt = await token();
    const res = await app.inject({
      method: 'GET',
      url: '/api/metrics/ai',
      headers: { authorization: `Bearer ${jwt}` },
    });
    expect(res.statusCode).toBe(200);
    expect(seenAuth).toBe(`Bearer ${jwt}`);
    await app.close();
  });

  it('preserves the query string', async () => {
    let seenUrl = '';
    const app = await appWith((url) => {
      seenUrl = url;
      return OK_JSON();
    });
    const jwt = await token();
    await app.inject({
      method: 'GET',
      url: '/api/tickets?status=open&limit=5',
      headers: { authorization: `Bearer ${jwt}` },
    });
    expect(seenUrl).toContain('status=open');
    expect(seenUrl).toContain('limit=5');
    await app.close();
  });

  it('forwards the request body', async () => {
    let seenBody = '';
    const app = await appWith((_url, init) => {
      seenBody = String(init?.body ?? '');
      return OK_JSON();
    });
    const jwt = await token();
    await app.inject({
      method: 'POST',
      url: '/api/tickets/1/ai/analyze',
      headers: { authorization: `Bearer ${jwt}` },
      payload: {},
    });
    expect(seenBody).toBe('{}');
    await app.close();
  });

  it('passes a backend 404 through', async () => {
    const app = await appWith(
      () => new Response(JSON.stringify({ detail: 'Ticket not found' }), { status: 404 }),
    );
    const jwt = await token();
    const res = await app.inject({
      method: 'GET',
      url: '/api/tickets/999',
      headers: { authorization: `Bearer ${jwt}` },
    });
    expect(res.statusCode).toBe(404);
    expect(res.json().detail).toBe('Ticket not found');
    await app.close();
  });

  it('keeps a backend 502 as 502 rather than reporting 503', async () => {
    // 502 from the backend means an AI provider failed, not that the gateway
    // itself is unavailable.
    const app = await appWith(
      () => new Response(JSON.stringify({ detail: 'provider down' }), { status: 502 }),
    );
    const jwt = await token();
    const res = await app.inject({
      method: 'POST',
      url: '/api/tickets/1/ai/suggest',
      headers: { authorization: `Bearer ${jwt}` },
      payload: {},
    });
    expect(res.statusCode).toBe(502);
    await app.close();
  });

  it('returns 503 when the backend is unreachable', async () => {
    const app = await appWith(UNAVAILABLE);
    const jwt = await token();
    const res = await app.inject({
      method: 'GET',
      url: '/api/tickets',
      headers: { authorization: `Bearer ${jwt}` },
    });
    expect(res.statusCode).toBe(503);
    await app.close();
  });

  it('does not forward a non-API path', async () => {
    // Reaches the proxy handler only with a valid token; the allowlist then
    // refuses the path without calling the backend.
    let called = false;
    const app = await appWith(() => {
      called = true;
      return OK_JSON();
    });
    const jwt = await token();
    const res = await app.inject({
      method: 'GET',
      url: '/internal/admin',
      headers: { authorization: `Bearer ${jwt}` },
    });
    expect(res.statusCode).toBe(404);
    expect(called).toBe(false);
    await app.close();
  });

  it('requires auth for GET on the ticket list path', async () => {
    // Regression: the public-path allowlist matched on path only, so
    // `GET /api/tickets` — the authenticated ticket list — skipped auth
    // entirely and was proxied to the backend with no credentials.
    let called = false;
    const app = await appWith(() => {
      called = true;
      return OK_JSON();
    });
    const res = await app.inject({ method: 'GET', url: '/api/tickets' });
    expect(res.statusCode).toBe(401);
    expect(called).toBe(false);
    await app.close();
  });

  it('requires auth for GET on the ticket detail path', async () => {
    const app = await appWith(OK_JSON);
    expect((await app.inject({ method: 'GET', url: '/api/tickets/1' })).statusCode).toBe(401);
    await app.close();
  });

  it('requires auth for GET on the metrics path', async () => {
    const app = await appWith(OK_JSON);
    expect((await app.inject({ method: 'GET', url: '/api/metrics/ai' })).statusCode).toBe(401);
    await app.close();
  });

  it('still forwards GET /api/tickets when a valid token is present', async () => {
    let called = false;
    const app = await appWith(() => {
      called = true;
      return OK_JSON();
    });
    const jwt = await token();
    const res = await app.inject({
      method: 'GET',
      url: '/api/tickets',
      headers: { authorization: `Bearer ${jwt}` },
    });
    expect(res.statusCode).toBe(200);
    expect(called).toBe(true);
    await app.close();
  });
});
