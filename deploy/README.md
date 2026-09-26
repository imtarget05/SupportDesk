# Production Deploy Runbook — Render API + Neon Postgres + Cloudflare Pages

> Live topology: `https://<app>.pages.dev` (Pages static) calls
> `https://<api>.onrender.com/api/...` (Render Docker) → **Neon Postgres**
> (serverless, external URL, `sslmode=require`).
> `docker-compose.yml` is local-only — never used in prod.

## 0. Prerequisites

- GitHub repo pushed to `main` (Render + Pages both deploy from git).
- Accounts: Render (free tier OK) + Cloudflare (free tier OK) + Neon (free tier OK).
- No secrets in git — every credential below is set on dashboards only.


## 1. Deploy API + Postgres (Render Web Service + Neon)

1. Create a Neon project → copy the **pooled connection string**, then adapt it:
   - Use the **direct** endpoint (drop `-pooler` from the hostname) for the app:
     migrations and SQLAlchemy pools behave like plain Postgres.
   - **Drop `channel_binding=require`** — psycopg2 rejects it as a startup option.
   - Keep `?sslmode=require` (TLS is mandatory on Neon).
   - Final form: `postgresql://<user>:<pw>@ep-xxxx.<region>.aws.neon.tech/<db>?sslmode=require`
2. Render dashboard → New → **Web Service** (Docker) from this repo
   (`backend/Dockerfile`, context repo root, health check `/api/health`).
3. Leave `CORS_ORIGINS` empty for now (set after Pages URL exists).
4. Wait for build (~7–18 min first time, torch dominates) → deploy.
   The first boot runs `alembic upgrade head` against Neon (creates all tables).

> Migration note: `backend/alembic/env.py` takes a **transaction-scoped**
> `pg_advisory_xact_lock` — a session-level lock silently breaks the migration
> transaction on Neon (DDL appears to run but never persists).

## 2. Render env reference

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon direct URL (§1); SQLite rejected in deployment. |
| `JWT_SECRET` | Random ≥32 chars. Never commit or bake into image. Rotate → all sessions logout. |
| `ENVIRONMENT` | `production` (enables Postgres-only + strict CORS fail-fast). |
| `ALEMBIC_MIGRATE` | `true` — idempotent `upgrade head` at boot. |
| `AI_PROVIDER` / `AI_EMBED_PROVIDER` | `cloudflare` / `bow` — real LLM `@cf/meta/llama-3.1-8b-instruct` via Workers AI. Needs `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (token must have **Workers AI → Run**; verify with `ai/run` → 200). A draft that fails the safety guardrail (e.g. promises a refund) returns **502 by design** — retry produces a fresh draft. `AI_EMBED_PROVIDER` currently accepts only `bow` in practice: the similar-ticket embedder always uses the deterministic 128-dim hashed bag-of-words `embed()`, so `hf` does not change persisted vectors. |
| `BOOTSTRAP_TOKEN` | Random ≥8 chars, set **before first deploy**; blank after bootstrap (§4). |
| `CORS_ORIGINS` | Set AFTER Pages deploy: `https://<app>.pages.dev`, then redeploy API. |
| `TICKET_CREATE_RATE_LIMIT` / `TICKET_CREATE_RATE_WINDOW_S` | Guest (unauthenticated) `POST /api/tickets` allowance, default `10` per `3600`s, keyed on the real client IP. Returned as `429` + `Retry-After`. `0` disables. Authenticated submitters are not charged. |

## 3. Verify API health + migrate

- Health: `GET https://<api>.onrender.com/api/health` → `{"status":"ok"}` (503 = DB/migration not ready, check logs for `upgrade head`).
- Migrations run automatically at boot; concurrent instances are lock-guarded.
- Neon free tier autosuspends the compute when idle — the first request after
  idle pays a few seconds of wake-up (the API surfaces it as a short 503).

## 4. Bootstrap the first agent (once)

Free plans have **no Shell/SSH access**, so `python -m app.seed` won't run there.
The app ships a one-time `/api/auth/bootstrap` endpoint guarded by `BOOTSTRAP_TOKEN`.

1. Before the first deploy, set `BOOTSTRAP_TOKEN=<random ≥8 chars>` (keep it secret;
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
2. After the service is live, create the agent with one call:

```bash
curl -X POST https://<api>.onrender.com/api/auth/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"name":"Support Admin","email":"admin@<your-domain>","password":"<strong-password>","setup_token":"<BOOTSTRAP_TOKEN>"}'
# -> 201 {access_token, user:{role:"agent"}}
```

3. The endpoint is strictly one-time: a second valid call returns 409 (agent exists),
   a wrong token returns 403. After bootstrapping, set `BOOTSTRAP_TOKEN=""` (or remove it)
   and redeploy so the route is disabled entirely.
4. Seeded demo accounts/tickets are local-only (`python -m app.seed`) — never run on prod.

## 5. Deploy frontend on Cloudflare Pages

1. Cloudflare dashboard → Pages → Connect repo → project name `<app>`.
2. Build settings: root `frontend/`, build `npm ci && npm run build`, output `dist`.
3. Build env: `VITE_API_URL=https://<api>.onrender.com` (Vite bakes it at build time — changing it requires rebuild).
4. Deploy → note the `https://<app>.pages.dev` URL. SPA routes (`/agent`, `/tickets/:id`) work via `public/_redirects` (`/* /index.html 200`).

## 6. Wire CORS and redeploy API

1. Render → service → Environment → set `CORS_ORIGINS=https://<app>.pages.dev` → Save (triggers redeploy).
2. Render terminates HTTPS at the boundary; no extra proxy config needed.

## 7. Prod smoke test (real user flow)

1. Open `https://<app>.pages.dev` → log in as agent.
2. Create a ticket (anonymous with email works too) → status `OPEN`, category `UNKNOWN`.
3. Agent opens ticket → AI suggest (stub) returns draft → send reply explicitly.
4. Drive lifecycle to `CLOSED` → verify new messages + 4 AI endpoints return 409.
5. Wrong-password login → 401; unauth `/api/tickets` → 401/403.

## 8. Rollback and secret rotation

- Rollback: Render → service → Deploys → redeploy previous commit. Keep Postgres; migrate forward only.
- Rotate `JWT_SECRET`: set new value → deploy → wait for old tokens to expire (`JWT_EXPIRE_MINUTES=720`) → done. All users are logged out on rotate.
- Rotate DB/provider credentials through Render controls; never commit values.

## 9. Known limits (launch)

- Free-tier sleep: API cold-starts ~30–60s after idle; Pages shows its loading state meanwhile. Upgrade to Starter if always-on is needed.
- First Docker build is slow (torch/sentence-transformers back the knowledge-base RAG index). Similar-ticket retrieval already runs on the dependency-free hashed bag-of-words embedder, so it needs no model download.
- `VITE_API_URL` is bake-time: any backend URL change needs a Pages rebuild + redeploy.

---

## 10. CI/CD (GitHub Actions)

Two workflows: **CI** (`.github/workflows/ci.yml` — already live) runs on every push to `main`/`feat/**` and PRs. **CD** (`.github/workflows/deploy.yml` — new) runs only after CI passes on `main`, or on manual dispatch.

### How it works

```
push to main → CI (pytest + build + leak scan + alembic + eval smoke)
                 │
                 └── success ─→ CD:
                     ├── backend job: build/push Docker image → GHCR → trigger Render redeploy → poll /api/health
                     └── frontend job: build → deploy to Cloudflare Pages
```

- CI jobs: `backend` (pytest, secret scan, alembic, eval smoke) + `frontend` (npm build + tests).
- CD triggers: `workflow_run` (CI completed successfully on `main`) + `workflow_dispatch` (manual trigger from Actions UI).
- Backend CD: builds the Docker image via `docker/build-push-action`, pushes to GHCR tagged with the full commit SHA + `latest`, then calls the Render deploy API, waits for the deploy to reach `live` (up to 20 minutes — free tier builds are slow), then polls `/api/health` until it returns 200.
- Frontend CD: deploys `frontend/dist` to the Cloudflare Pages project **`supportdesk`** (live at `https://supportdesk-aht.pages.dev`) via `wrangler pages deploy` on every CI-green push.
- **Fail-closed**: if CI fails, CD does not run. If Render redeploy fails, the job fails and no further steps run.

### Required repository secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Where to get it | Used by |
|---|---|---|
| `RENDER_API_KEY` | Render dashboard → Account Settings → API Keys → **Create API Key** (needs **Service → Deploy** permission) | `deploy.yml` backend job — redeploy + health poll |
| `RENDER_SERVICE_ID` | Render dashboard → your service → scroll to bottom → **Service ID** (looks like `srv-xxxx`) | `deploy.yml` backend job — target service |
| `CLOUDFLARE_API_TOKEN` | Cloudflare dashboard → My Profile → API Tokens → **Create Token** → choose “Cloudflare Pages: Edit” template (or custom: `Pages → Edit`, `Account → Cloudflare Pages` read) | `deploy.yml` frontend job — Pages deploy |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard URL: `https://dash.cloudflare.com/<account-id>` — copy the long hex ID | `deploy.yml` frontend job — Pages project scope |

> The GitHub PAT that was pasted in chat **cannot** deploy to Render — Render's deploy API uses `RENDER_API_KEY`, not a GitHub token. If you still have that PAT active, **revoke/rotate it** in your GitHub settings. Never commit a PAT, even in a workaround script.

### First-time setup

1. Add the four secrets above.
2. Push a commit to `main` (or trigger manually from the Actions tab) → CI runs first.
3. When CI turns green, CD fires automatically and:
   - Backend image is built/pushed to GHCR (first push takes a few minutes due to torch).
   - Render redeploys the service from the new image.
   - `/api/health` is polled until it returns `{"status":"ok"}`.
   - Frontend is rebuilt and deployed to Cloudflare Pages.
4. If any secret is missing, the relevant CD step is skipped with a clear log message — the workflow won't fail on missing secrets, it just won't deploy.

### Manual trigger

Go to **Actions → CD → Run workflow → Branch: main → Run workflow**. Useful for re-deploying without a code change (e.g. after changing env vars in Render dashboard).

### Rollback via CD

Render keeps a deploy history: **Actions → CD → find the last successful run → re-run** will redeploy the same image. To go back to an older image, tag it manually in GHCR or use Render's deployment history UI.
