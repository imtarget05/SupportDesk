import { z } from 'zod';

/**
 * Gateway configuration, validated once at startup.
 *
 * Parsing the environment through a schema means a malformed value fails at
 * boot with a named error, instead of surfacing later as a confusing `undefined`
 * inside a request handler.
 */
const schema = z.object({
  PORT: z.coerce.number().int().positive().default(8080),
  HOST: z.string().min(1).default('0.0.0.0'),
  BACKEND_URL: z.string().url().default('http://localhost:8000'),
  /** Per-request timeout for a call to the Python service. */
  BACKEND_TIMEOUT_MS: z.coerce.number().int().positive().default(30_000),
  /**
   * Secret used to verify the HS256 JWT the backend issued. The gateway reads
   * the same symmetric key; it never mints tokens.
   */
  JWT_SECRET: z.string().min(32, 'JWT_SECRET must be at least 32 characters'),
  /** Comma-separated allowlist. Empty means same-origin only. */
  CORS_ORIGINS: z.string().default(''),
  /** Max JSON body the gateway will buffer, in bytes. */
  MAX_BODY_BYTES: z.coerce.number().int().positive().default(1_048_576),
  // `silent` disables logging; useful in tests and when logs go elsewhere.
  LOG_LEVEL: z.enum(['debug', 'info', 'warn', 'error', 'silent']).default('info'),
});

export type Config = z.infer<typeof schema>;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const parsed = schema.safeParse(env);
  if (!parsed.success) {
    const details = parsed.error.issues
      .map((issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`)
      .join('; ');
    throw new Error(`Invalid gateway configuration: ${details}`);
  }
  return parsed.data;
}

export function corsOrigins(config: Config): string[] {
  return config.CORS_ORIGINS.split(',')
    .map((origin) => origin.trim())
    .filter((origin) => origin.length > 0);
}