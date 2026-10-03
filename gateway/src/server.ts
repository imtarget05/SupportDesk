import Fastify, { type FastifyInstance, type FastifyReply, type FastifyRequest } from 'fastify';
import { extractBearer, verifyToken, type TokenClaims } from './auth.js';
import { BackendClient, BackendUnavailable, detailFrom, mapStatus } from './backend.js';
import { corsOrigins, type Config } from './config.js';

/** Requests the gateway forwards to the Python service. */
const PROXY_PREFIX = '/api';

/** Paths served by the gateway itself rather than forwarded. */
const LOCAL_PATHS = new Set(['/healthz', '/readyz']);

declare module 'fastify' {
  interface FastifyRequest {
    claims?: TokenClaims;
  }
}

export interface BuildOptions {
  config: Config;
  backend?: BackendClient;
}

export async function buildServer({ config, backend }: BuildOptions): Promise<FastifyInstance> {
  const client =
    backend ??
    new BackendClient({
      baseUrl: config.BACKEND_URL,
      timeoutMs: config.BACKEND_TIMEOUT_MS,
    });

  const app = Fastify({
    logger: { level: config.LOG_LEVEL },
    bodyLimit: config.MAX_BODY_BYTES,
  });

  // Paths the gateway answers itself need no auth: they expose no data.
  app.get('/healthz', async () => ({ status: 'ok' }));

  app.get('/readyz', async (_request, reply) => {
    try {
      const result = await client.health();
      if (result.status >= 500) {
        return reply.code(503).send({ status: 'degraded', backend: result.status });
      }
      return { status: 'ready', backend: result.status };
    } catch (error) {
      return reply.code(503).send({
        status: 'unavailable',
        detail: error instanceof Error ? error.message : String(error),
      });
    }
  });

  /**
   * Authenticate before forwarding. A 401 is returned here rather than being
   * passed through, so the gateway never spends an upstream round trip on an
   * unauthenticated call.
   */
  app.addHook('onRequest', async (request: FastifyRequest, reply: FastifyReply) => {
    if (isUnauthenticated(request.url, request.method)) return;

    const token = extractBearer(request.headers.authorization);
    if (!token) {
      return reply.code(401).send({ detail: 'Missing bearer token' });
    }
    const verified = await verifyToken(token, config.JWT_SECRET);
    if (!verified.ok) {
      return reply.code(401).send({ detail: `Invalid token: ${verified.reason}` });
    }
    request.claims = verified.claims;
  });

  // Allowlist the proxy target: a path is forwarded only when it starts with
  // the API prefix, so a crafted URL can never reach another origin.
  app.all(`${PROXY_PREFIX}/*`, async (request, reply) => {
    const url = request.url.split('?')[0] ?? '';
    if (!url.startsWith(`${PROXY_PREFIX}/`)) {
      return reply.code(404).send({ detail: 'Not found' });
    }

    const method = request.method;
    if (!FORWARDABLE.has(method)) {
      return reply.code(405).send({ detail: `Method ${method} is not proxied` });
    }

    const token = extractBearer(request.headers.authorization);
    const query = request.url.includes('?') ? `?${request.url.split('?')[1] ?? ''}` : '';

    try {
      const result = await client.request(method, `${url}${query}`, {
        token: token ?? undefined,
        body: hasBody(method) ? request.body : undefined,
      });
      return reply.code(mapStatus(result.status)).send(result.body);
    } catch (error) {
      if (error instanceof BackendUnavailable) {
        // The gateway's own upstream is gone; that is a 503, distinct from a
        // backend that answered with 502 because an AI provider failed.
        return reply.code(503).send({ detail: error.message });
      }
      throw error;
    }
  });

  app.setNotFoundHandler(async (request, reply) => {
    if (request.url.startsWith(PROXY_PREFIX)) {
      return reply.code(404).send({ detail: detailFrom(null, 'Not found') });
    }
    return reply.code(404).send({ detail: 'Not found' });
  });

  const origins = corsOrigins(config);
  if (origins.length > 0) {
    app.addHook('onRequest', async (request, reply) => {
      const origin = request.headers.origin;
      if (origin && origins.includes(origin)) {
        void reply.header('access-control-allow-origin', origin);
        void reply.header('vary', 'origin');
      }
      if (request.method === 'OPTIONS') {
        void reply.header('access-control-allow-methods', 'GET,POST,PATCH,PUT,DELETE');
        void reply.header('access-control-allow-headers', 'authorization,content-type');
        return reply.code(204).send();
      }
    });
  }

  return app;
}

const FORWARDABLE = new Set(['GET', 'POST', 'PATCH', 'PUT', 'DELETE']);

function hasBody(method: string): boolean {
  return method === 'POST' || method === 'PATCH' || method === 'PUT';
}

/**
 * Paths the gateway answers itself, plus the public entry points.
 *
 * `POST /api/tickets` is public because the contact form must work before
 * signup — but only for POST. A `GET` of the same path is the ticket list,
 * which is authenticated, so the check is on the method as well as the path.
 */
function isUnauthenticated(url: string, method: string): boolean {
  const path = url.split('?')[0] ?? '';
  if (LOCAL_PATHS.has(path)) return true;
  if (method !== 'POST') return false;
  return (
    path === '/api/tickets' || path === '/api/auth/login' || path === '/api/auth/register'
  );
}

export { BackendClient };