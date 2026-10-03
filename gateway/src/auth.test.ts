import { SignJWT } from 'jose';
import { describe, expect, it } from 'vitest';
import { extractBearer, verifyToken } from './auth.js';

const SECRET = 's'.repeat(32);
const OTHER_SECRET = 'o'.repeat(32);

async function makeToken(
  payload: Record<string, unknown>,
  secret = SECRET,
  expiresIn = '5m',
): Promise<string> {
  return new SignJWT(payload)
    .setProtectedHeader({ alg: 'HS256' })
    .setExpirationTime(expiresIn)
    .sign(new TextEncoder().encode(secret));
}

describe('extractBearer', () => {
  it('reads a well-formed header', () => {
    expect(extractBearer('Bearer abc.def.ghi')).toBe('abc.def.ghi');
  });

  it('is case-insensitive on the scheme', () => {
    expect(extractBearer('bearer abc')).toBe('abc');
  });

  it('returns null for a missing header', () => {
    expect(extractBearer(undefined)).toBeNull();
  });

  it('returns null for a non-bearer scheme', () => {
    expect(extractBearer('Basic dXNlcjpwYXNz')).toBeNull();
  });

  it('returns null when the token is empty', () => {
    expect(extractBearer('Bearer')).toBeNull();
  });
});

describe('verifyToken', () => {
  it('accepts a valid agent token', async () => {
    const token = await makeToken({ sub: '1', role: 'agent', name: 'Ada' });
    const result = await verifyToken(token, SECRET);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.claims.role).toBe('agent');
      expect(result.claims.sub).toBe('1');
      expect(result.claims.name).toBe('Ada');
    }
  });

  it('rejects a token signed with another secret', async () => {
    const token = await makeToken({ sub: '1', role: 'agent' }, OTHER_SECRET);
    const result = await verifyToken(token, SECRET);
    expect(result.ok).toBe(false);
  });

  it('rejects an expired token', async () => {
    const token = await makeToken({ sub: '1', role: 'agent' }, SECRET, '-1m');
    expect((await verifyToken(token, SECRET)).ok).toBe(false);
  });

  it('rejects a token missing the role claim', async () => {
    const token = await makeToken({ sub: '1' });
    const result = await verifyToken(token, SECRET);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toMatch(/claims/);
  });

  it('rejects garbage instead of throwing', async () => {
    const result = await verifyToken('not-a-jwt', SECRET);
    expect(result.ok).toBe(false);
  });

  it('rejects an unsigned (alg=none) token', async () => {
    const header = Buffer.from(JSON.stringify({ alg: 'none', typ: 'JWT' })).toString('base64url');
    const payload = Buffer.from(
      JSON.stringify({ sub: '1', role: 'agent', exp: Math.floor(Date.now() / 1000) + 300 }),
    ).toString('base64url');
    const result = await verifyToken(`${header}.${payload}.`, SECRET);
    expect(result.ok).toBe(false);
  });
});