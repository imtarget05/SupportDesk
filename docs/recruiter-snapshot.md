# Recruiter snapshot — SupportDesk

## What it is

**SupportDesk — an AI-native support operations platform.** A React SPA, a
TypeScript API gateway, and a FastAPI AI service. The AI **assists agents, never
decides**: it triages tickets, retrieves policy evidence, calls read-only tools,
and drafts a reply — and never sends anything, changes ticket state, or promises
a refund on its own authority.

## Why it stands out

- **Measured, not assumed.** An eval harness measures four things on 92 labeled
  tickets — classification accuracy (93.5%), schema validity (100%), guardrail
  trip rate (21.7%), and cost/latency per ticket — and CI fails the build on a
  regression against `evaluation/baseline.json`. Baselines on the same data are
  reported too, including the ones that beat the default approach.
- **Cost and latency are first-class.** Every LLM call is persisted with token
  counts, a versioned-rate-table USD cost, latency and outcome. `GET
  /api/metrics/ai` gives per-model p50/p95, error rate and cost per successful
  call. An unpriced model reports `null`, not a fabricated zero.
- **Durable AI work — defined, not yet runnable.** The ticket pipeline is
  written as a Temporal workflow with a real human-approval gate. Be precise:
  the definition, signals and query are tested, but a Temporal worker cannot
  currently start (activities lack `@activity.defn`, and the workflow body is not
  sandbox-safe), and nothing has run against a cluster. What works today is the
  in-process runner, which is explicitly not durable. `docs/interview-qa.md`
  carries the evidence.
- **Tool calling with a boundary.** The agent loop calls read-only tools whose
  arguments are validated by the same Pydantic model that generates the
  advertised JSON schema. Loop detection and a step ceiling stop runaway runs.
  Native provider tool calling is not wired up yet — the offline path uses a
  deterministic planner.
- **Standards, not reinvention.** LangGraph (`StateGraph` + checkpointing +
  `interrupt`), MCP (tools published over the protocol), Qdrant (vector search),
  OpenTelemetry GenAI spans (OTLP, so Langfuse/Tempo can consume them).
- **Guardrails fail closed.** A blocked draft returns `502` and leaves the ticket
  untouched. The checks cover commitments, ungrounded facts, prompt injection —
  and internal detail, so a tool error cannot reach a customer.
- **Honest engineering record.** This project's own audit once flagged three
  false claims. All three were fixed in code and regression-tested, and two more
  real defects (a gateway auth bypass, a missing internal-detail guardrail) were
  found by tests and fixed. See the end of `docs/RECRUITER-EVIDENCE.md`.

## Numbers

331 backend tests · 56 gateway tests · 15 frontend tests · 6 Alembic migrations ·
all offline, no API key.

## Caveats, stated plainly

Redis is not used (rate limiting is per-instance); the Temporal runtime path
does not run; Langfuse is OTel-compatible rather than hosted; native provider
tool calling is not wired up; there is no production traffic. Full detail,
requirement by requirement, is in `docs/JD-MAP.md`, and the interview-ready
answers are in `docs/interview-qa.md`.

## Repo hygiene

No AI-harness files; `.venv` and `node_modules` gitignored; credentials in
`backend/.env` (gitignored) with `.env.example` showing the shape; normal commit
history.
