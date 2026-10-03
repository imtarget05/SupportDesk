import { describe, expect, it } from 'vitest';
import { corsOrigins, loadConfig } from './config.js';

const VALID_SECRET = 'x'.repeat(32);

describe('loadConfig', () => {
  it('applies documented defaults', () => {
    const config = loadConfig({ JWT_SECRET: VALID_SECRET } as NodeJS.ProcessEnv);
    expect(config.PORT).toBe(8080);
    expect(config.BACKEND_URL).toBe('http://localhost:8000');
    expect(config.BACKEND_TIMEOUT_MS).toBe(30_000);
    expect(config.LOG_LEVEL).toBe('info');
  });

  it('rejects a short JWT secret', () => {
    expect(() => loadConfig({ JWT_SECRET: 'too-short' } as NodeJS.ProcessEnv)).toThrow(
      /JWT_SECRET/,
    );
  });

  it('rejects a missing JWT secret', () => {
    expect(() => loadConfig({} as NodeJS.ProcessEnv)).toThrow(/JWT_SECRET/);
  });

  it('names the offending field in the error', () => {
    try {
      loadConfig({ JWT_SECRET: 'short' } as NodeJS.ProcessEnv);
      expect.unreachable('should have thrown');
    } catch (error) {
      expect((error as Error).message).toMatch(/Invalid gateway configuration/);
      expect((error as Error).message).toMatch(/JWT_SECRET/);
    }
  });

  it('rejects a non-numeric port', () => {
    expect(() =>
      loadConfig({ JWT_SECRET: VALID_SECRET, PORT: 'not-a-port' } as NodeJS.ProcessEnv),
    ).toThrow(/PORT/);
  });

  it('rejects an invalid log level', () => {
    expect(() =>
      loadConfig({ JWT_SECRET: VALID_SECRET, LOG_LEVEL: 'chatty' } as NodeJS.ProcessEnv),
    ).toThrow(/LOG_LEVEL/);
  });

  it('coerces numeric strings', () => {
    const config = loadConfig({
      JWT_SECRET: VALID_SECRET,
      PORT: '9000',
      BACKEND_TIMEOUT_MS: '5000',
    } as NodeJS.ProcessEnv);
    expect(config.PORT).toBe(9000);
    expect(config.BACKEND_TIMEOUT_MS).toBe(5000);
  });
});

describe('corsOrigins', () => {
  it('is empty when unset', () => {
    expect(corsOrigins(loadConfig({ JWT_SECRET: VALID_SECRET } as NodeJS.ProcessEnv))).toEqual([]);
  });

  it('splits and trims a list', () => {
    const config = loadConfig({
      JWT_SECRET: VALID_SECRET,
      CORS_ORIGINS: 'http://a.test, http://b.test ',
    } as NodeJS.ProcessEnv);
    expect(corsOrigins(config)).toEqual(['http://a.test', 'http://b.test']);
  });

  it('drops empty entries', () => {
    const config = loadConfig({
      JWT_SECRET: VALID_SECRET,
      CORS_ORIGINS: ',,http://a.test,',
    } as NodeJS.ProcessEnv);
    expect(corsOrigins(config)).toEqual(['http://a.test']);
  });
});