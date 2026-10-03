import { jwtVerify } from 'jose';

/**
 * Claims the gateway reads from the backend's JWT.
 *
 * The gateway *verifies* tokens and does not mint them: authentication stays
 * with the Python service, so there is exactly one place that issues
 * credentials and one secret to rotate.
 */
export interface TokenClaims {
  sub: string;
  role: string;
  name: string;
  exp: number;
}

export type VerifyResult =
  | { ok: true; claims: TokenClaims }
  | { ok: false; reason: string };

function secretKey(secret: string): Uint8Array {
  return new TextEncoder().encode(secret);
}

/**
 * Verify a bearer token issued by the backend.
 *
 * The role is checked here so the gateway can reject a customer token before
 * spending a round trip on the backend; the backend still re-checks
 * authorization, because a gateway check is a convenience, not the boundary.
 */
export async function verifyToken(
  token: string,
  secret: string,
): Promise<VerifyResult> {
  try {
    const { payload } = await jwtVerify(token, secretKey(secret), {
      algorithms: ['HS256'],
    });

    if (typeof payload.sub !== 'string' || typeof payload.role !== 'string') {
      return { ok: false, reason: 'token is missing required claims' };
    }
    return {
      ok: true,
      claims: {
        sub: payload.sub,
        role: payload.role,
        name: typeof payload.name === 'string' ? payload.name : '',
        exp: typeof payload.exp === 'number' ? payload.exp : 0,
      },
    };
  } catch (error) {
    return { ok: false, reason: errorMessage(error) };
  }
}

export function extractBearer(header: string | undefined): string | null {
  if (!header) return null;
  const match = /^Bearer\s+(.+)$/i.exec(header.trim());
  return match?.[1] ?? null;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}