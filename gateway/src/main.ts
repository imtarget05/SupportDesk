import { loadConfig } from './config.js';
import { buildServer } from './server.js';

/**
 * Gateway entry point.
 *
 * Config is validated before the server starts, so a bad environment fails with
 * a named error instead of a half-booted process.
 */
async function main(): Promise<void> {
  const config = loadConfig();
  const app = await buildServer({ config });

  const shutdown = async (signal: string): Promise<void> => {
    app.log.info(`${signal} received, closing gateway`);
    await app.close();
    process.exit(0);
  };
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('SIGTERM', () => void shutdown('SIGTERM'));

  await app.listen({ port: config.PORT, host: config.HOST });
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});