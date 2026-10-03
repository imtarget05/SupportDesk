# JD Map — Applied AI Engineer (Everfit Technologies Việt Nam)

Each row maps a requirement from the posted role to what is actually in this
repository, with the file to open and the command that proves it.

**How to read this honestly.** Claims marked **Verified** were run in this
repository. Claims marked **Not implemented** are gaps, listed because a good
candidate is candid about them. Nothing here is asserted without a way to check.

Run everything yourself:

```bash
cd backend && .venv/bin/python -m pytest -q       # backend suite, offline
cd gateway && npm ci && npm run typecheck && npm test
cd frontend && npm ci && npm test
AI_PROVIDER=stub python evaluation/eval_suite.py --check
```

---

## 1. Role profile

| JD requirement | Status | Evidence |
|---|---|---|
| ~70% LLM systems (agent workflows, RAG, tool calling, evals) | **Verified** | `app/services/tool_agent.py`, `app/services/vector_store.py` + `knowledge_base.py`, `app/workflows/definitions.py`, `evaluation/eval_suite.py` |
| ~30% backend services | **Verified** | FastAPI + SQLAlchemy + Alembic, 6 REST routers, Postgres in prod |
| Python/FastAPI | **Verified** | `backend/app/main.py`, `backend/app/api/*.py` |
| TypeScript backend ("mainly in Node") | **Verified** | `gateway/` — Fastify 5, strict TS, 56 Vitest tests, CI job `gateway` |
| Shipped an LLM feature to production via OpenAI/Anthropic | **Partial** | Providers implemented and wired, but this is a portfolio with no production traffic. Say "implemented, tested and deployable", never "shipped to N users" |

## 2. Durable workflows on Temporal

> **Correction (2026-10-03).** This table previously read **Verified** for the
> Temporal row. Running the SDK proved that wrong: a Temporal **worker cannot
> start**. Two defects, both reproduced:
>
> 1. Activities are passed as plain callables to `Worker(...)`, but `temporalio`
>    requires `@activity.defn` and async activities. Starting the worker raises
>    `TypeError: Activity activity_classify missing attributes, was it decorated
>    with @activity.defn?`
> 2. The workflow body is not sandbox-safe. Validating the workflow raises
>    `RestrictedWorkflowAccessError: Cannot access pathlib.Path.resolve.__call__
>    from inside a workflow`, because `stage_timeout_seconds()` reads
>    `app.config.settings`, whose import touches the filesystem.
>
> `docs/interview-qa.md` (Temporal section) carries the full evidence and the fix
> path. Statuses below are corrected accordingly.

| JD requirement | Status | Evidence |
|---|---|---|
| Multi-step agent processes | **Implemented, not runnable on Temporal** | `app/workflows/definitions.py` defines the stages (classify → retrieve → draft → approval → send) and the in-process runner executes them under test. The Temporal runtime path does not start — see the correction above |
| Survive restarts | **Not implemented** | Nothing durable exists yet: the worker cannot start, and the in-process runner is explicitly not durable (no history, no restart survival) |
| Retries and partial failures | **Verified (local runner only)** | `pipeline.py::run_stage()` retries to `MAX_STAGE_ATTEMPTS`; `tests/test_workflows.py::test_transient_failure_is_retried`, `::test_permanent_failure_raises_after_the_ceiling`. Temporal's own retry policy is not configured |
| Human approval that suspends the run | **Verified (local runner); Temporal signal path untested** | `definitions.py` declares `wait_condition` and `approve`/`reject` signals; `tests/test_workflows.py::test_workflow_exposes_approval_signals` asserts they are registered. The suspend/resume round trip is exercised through the local runner, not a live worker |
| Workflow ids and duplicate protection | **Implemented, untested against a server** | `_reuse_policy()` returns `ALLOW_DUPLICATE_FAILED_ONLY`; `tests/test_workflows.py::test_reuse_policy_allows_a_retry_only_after_failure` |
| Temporal in production | **Not implemented** | `TEMPORAL_ENABLED=false` by default; `docker-compose.yml` ships a `temporal` service, but nothing has run against it |

## 3. Reliable AI pipelines

| JD requirement | Status | Evidence |
|---|---|---|
| Validation | **Verified** | `AnalysisResult` Pydantic model; malformed output raises and leaves the ticket untouched — `tests/test_ai_service.py::test_malformed_llm_output_502_ticket_unchanged` |
| Retries | **Verified** | `ai_service._call_with_retry()`; `::test_retry_*` |
| Fallbacks | **Verified** | Provider chain (stub/cloudflare/openai/anthropic); Qdrant falls back to LlamaIndex — `tests/test_vector_store.py::test_knowledge_base_falls_back_when_qdrant_raises` |
| Guardrails | **Verified** | `app/services/guardrails.py` — commitments, ungrounded claims, prompt injection, **internal-detail leakage**; `tests/test_guardrails.py` |
| Observability | **Verified** | `app/services/tracing.py`, `app/services/telemetry.py`, `GET /api/metrics/ai`, `GET /api/metrics/ai/recent` |
## 4. Evals and monitoring

| JD requirement | Status | Evidence |
|---|---|---|
| Define how AI features are measured | **Verified** | `evaluation/eval_suite.py` measures four groups: classification, schema validity, guardrail trip rate, cost/latency |
| Catch regressions before users do | **Verified** | `--check` compares against `evaluation/baseline.json`; CI step "Eval regression gate" fails on >2pp accuracy drop, guardrail spike, or p95 blow-up |
| Cost visibility | **Verified** | `app/services/pricing.py` (versioned rate table), `ai_call_traces.cost_usd`, `GET /api/metrics/ai` |

## 5. Production integrations

| JD requirement | Status | Evidence |
|---|---|---|
| OpenAI | **Verified** | `ai_service.OpenAIProvider` — chat completions + `usage` parsing; `tests/test_ai_providers.py::test_openai_provider_reads_usage` |
| Anthropic | **Verified** | `ai_service.AnthropicProvider` — Messages API with `input_tokens`/`output_tokens`; `::test_anthropic_parses_usage_and_text` |
| Token & cost per call | **Verified** | `ai_call_traces` table; `tests/test_tracing.py` |
| Latency p50/p95 | **Verified** | `GET /api/metrics/ai` → `by_model[].p50_latency_ms` / `p95_latency_ms` |

## 6. Core backend competence

| JD requirement | Status | Evidence |
|---|---|---|
| REST APIs | **Verified** | `backend/app/api/` — auth, tickets, ai, metrics, dashboard, webhooks |
| PostgreSQL | **Verified** | `DATABASE_URL` in prod, 6 Alembic migrations, optimistic concurrency on ticket status |
| Async I/O | **Partial** | `fetch` with `AbortSignal` in the gateway, async throughout the TypeScript service. On the Python side the AI layer uses sync `httpx` calls, and the Temporal workflow body is `async` but its activities are currently sync callables that the SDK rejects — see section 2 |
| Redis | **Not implemented** | Rate limiting is in-process (`app/rate_limit.py`) — per-instance, and therefore under-counts behind multiple workers |
| Message queues | **Partial** | Temporal's task queue carries the AI work; no Kafka/RabbitMQ/SQS |
| Testing depth | **Verified** | 331 backend test functions, 56 gateway tests, 15 frontend tests |

## 7. Stack named in the JD

| JD item | Status | Evidence |
|---|---|---|
| Pydantic | **Verified** | Provider outputs, tool arguments, API schemas, workflow inputs |
| LangGraph | **Verified (now genuinely)** | `app/services/graph_workflow.py` — real `StateGraph` with `MemorySaver` and `interrupt()`/`Command(resume=...)`. **Previously a false claim**: the module called itself LangGraph while importing nothing from it. `tests/test_graph_workflow.py::TestLangGraphIsReal` now asserts the framework is in the execution path |
| Qdrant | **Verified** | `app/services/vector_store.py` — embedded or server mode, chunking, cosine retrieval; `tests/test_vector_store.py` |
| Langfuse | **Partial** | `app/services/telemetry.py` emits OpenTelemetry GenAI spans, which Langfuse ingests over OTLP. Say "OTel GenAI traces (Langfuse-compatible)", not "we run Langfuse" |
| MCP | **Verified** | `app/mcp_server.py` exposes the four tools over the Model Context Protocol; `tests/test_mcp_server.py` asserts MCP's advertised tools match the in-process registry |
| AWS | **Not implemented** | Deployment target is Render + Cloudflare Pages. Compose is cloud-neutral |
| Docker | **Verified** | `docker-compose.yml` (db, backend, gateway, frontend, qdrant, temporal); multi-stage Dockerfiles |
| AI coding tools used daily | **Claim** | `docs/spec.md` §12 — used for boilerplate, tests and refactoring, with every change reviewed and verified by running the suite |

## 8. Vector search

| JD requirement | Status | Evidence |
|---|---|---|
| Qdrant | **Verified** | `QdrantVectorStore` with COSINE distance; chunks indexed from the knowledge base, retrieval returns scored hits |
| Embeddings honour their configuration | **Verified (fixed)** | `retrieval_service.ensure_embedding` used to call the bag-of-words embedder directly, so `AI_EMBED_PROVIDER=hf` changed nothing on the persistence path. `tests/test_retrieval.py::test_ensure_embedding_honours_hf_provider` is the regression test |

## 9. Security and governance

| JD requirement | Status | Evidence |
|---|---|---|
| Identity / access control | **Verified** | JWT auth; the gateway verifies tokens before proxying; agent-only routes — `gateway/src/server.test.ts` |
| Auditability | **Verified** | Every AI call persisted to `ai_call_traces`; every tool call appended to the registry audit log |
| Data boundaries | **Verified** | Agent-only metrics; ticket text treated as untrusted data in prompts; per-IP limit on the public endpoint |

## 10. Business framing

| JD requirement | Status | Evidence |
|---|---|---|
| Support automation | **Verified** | The product is a support ticket system; the AI layer does triage, retrieval and drafting |
---

## Verified numbers

Run in this repository; the commands are at the top of this file.

- **Backend suite**: 331 test functions, all passing offline (`AI_PROVIDER=stub`,
  no API key, no external services).
- **Gateway**: 56 Vitest tests; `tsc --noEmit` clean under `strict`,
  `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`.
- **Frontend**: 15 Vitest tests across 7 spec files.
- **Classification eval** (92 labeled tickets, stub provider): accuracy 93.5%,
  macro-F1 0.94, schema validity 100%, guardrail block rate 21.7%.
- **Baselines on the same 92 tickets** (`docs/model-comparison.md`): rule-based
  stub 93.5% / 0.94, TF-IDF + LogReg 1.0 / 1.0, DistilBERT fine-tune 1.0 / 1.0,
  Llama 3.1 8B 79.3% / 0.78.

Those 1.0 scores come from a 19-sample validation split drawn from 92 labels.
The number is dataset-specific, not a benchmark result. `docs/interview-qa.md` §5
explains how to answer that question directly rather than deflecting.

## Gaps, stated plainly

These are real. Each is a scope decision or an unfinished item, and none is
disguised by the code.

1. **Redis is not used.** Rate limiting is in-process, so it is per-instance and
   under-counts behind more than one worker. The per-IP limit on guest
   submissions is the one that matters, and it would need Redis or an equivalent
   shared store.
2. **The Temporal runtime path does not run.** The workflow definition, its
   signals, its query and the in-process runner are real and tested. But a
   Temporal worker cannot start: the activities are not decorated with
   `@activity.defn` and the workflow body is not sandbox-safe. Nothing has run
   against a Temporal cluster. See the correction note in section 2 and the
   Temporal section of `docs/interview-qa.md`.
3. **Langfuse is OTel-compatible, not Langfuse-hosted.** No hosted Langfuse
   project is configured.
4. **No AWS.** Deployment is Render + Cloudflare Pages.
5. **No production traffic.** "Shipped to production" should be phrased as
   "implemented, tested and deployable".
6. **The knowledge base is two documents.** RAG works end to end, but a corpus
   this small does not stress retrieval quality.
7. **Native provider tool calling is not implemented.** The agent loop uses a
   deterministic planner offline; OpenAI/Anthropic tool-call formats are not yet
   normalized into the loop's `ToolCall` shape.
8. **`docker compose up` has not been run against this tree.** The Compose file
   is valid and `JWT_SECRET` fails fast as intended, but a valid config proves
   nothing about the services starting.

## Corrections made to earlier claims

`docs/RECRUITER-EVIDENCE.md` had flagged three inaccurate claims in the previous
version of this project. All three are fixed in code, not only in prose:

| Earlier claim | What it actually was | Now |
|---|---|---|
| "LangGraph workflow" | Imported nothing from `langgraph`; a straight-line sequence | Real `StateGraph` + `MemorySaver` + `interrupt()`/`Command(resume=...)` |
| "sentence-transformers embeddings" | `ensure_embedding` bypassed the setting; every persisted vector was the 128-dim bag-of-words embedder | Provider honoured on the persistence path; each vector records its producer and width, and mismatched vectors are recomputed |
| "OpenTelemetry observability" | Did not exist; only hand-rolled in-process counters | Real OTel GenAI spans plus a durable `ai_call_traces` table |

Two further defects were found by tests written during this work, and are
regression-tested:

- The gateway's public-path allowlist matched on path only, so `GET /api/tickets`
  — the authenticated ticket list — skipped authentication entirely.
  `gateway/src/server.test.ts::requires auth for GET on the ticket list path`.
- The output guardrails did not block internal detail. A tool error echoed into a
  draft ("OperationalError: no such table: tickets") could have reached a
  customer. `tests/test_guardrails.py::test_internal_details_are_rejected`.
| Fintech / KYC / risk | **Not implemented** | This project is support-desk domain, not financial services |
| Health / fitness / personalization | **Not implemented** | Not this repository's domain |