# Interview Q&A — SupportDesk

Every answer here is reproducible from this repository. Each names the file or
test that backs it, and states a limitation where one exists. Where the
repository does **not** prove something, the answer says so — an interview is
exactly the wrong place to discover that gap yourself.

Re-run the anchors before an interview so the numbers you quote are current:

```bash
cd backend  && AI_PROVIDER=stub .venv/bin/python -m pytest -q   # 331 tests, offline
cd gateway && npm run typecheck && npm test                     # 56 tests, strict TS
cd frontend && npm test                                          # 15 tests
AI_PROVIDER=stub python evaluation/eval_suite.py --check        # exit 0
```

Related: [`JD-MAP.md`](JD-MAP.md) (requirement → code),
[`COST-LATENCY.md`](COST-LATENCY.md) (measured vs projected),
[`ARCHITECTURE.md`](ARCHITECTURE.md) (service ownership),
[`RECRUITER-EVIDENCE.md`](RECRUITER-EVIDENCE.md) (audit trail).

Format per question: short answer → depth → evidence → limitation → follow-up.

---

---

## Architecture


### Q: Walk me through one request end-to-end.

**Short answer.** A browser request hits the TypeScript gateway, which verifies
the HS256 JWT before anything else; the request is proxied to the FastAPI
backend, which authorizes again, runs the domain logic, and — for AI endpoints —
calls a provider inside a trace wrapper.

**Deeper:**
- Gateway (`gateway/src/server.ts`): an `onRequest` hook verifies the token with
  `jose`, then an `/api/*` allowlist route forwards to the backend.
- The backend authorizes independently. A gateway check is a convenience; the
  boundary is the backend.
- For an AI endpoint, `ai_service` wraps the provider call in
  `tracing.trace_call(...)`, so a row in `ai_call_traces` exists whether the call
  succeeds, fails, or trips a guardrail.
- Provider output is parsed into a Pydantic model **before** anything is
  persisted. A parse failure raises, leaving the ticket untouched.
- Guardrails then run on the output; a violation returns `502` and still writes
  nothing.

**Evidence.** `tests/test_malformed_llm_output_502_ticket_unchanged`;
`tests/test_tracing.py::test_analyze_ticket_persists_a_trace`.

**Limitation.** No request has ever been served under production traffic.

**Follow-up.** "If the gateway is down, does the backend still work?" — Yes,
directly. The gateway is the caller-facing boundary, not a dependency of the
directly. The gateway is the caller-facing boundary, not a dependency of the
domain logic.

### Q: Why a Node gateway plus a Python backend?

**Short answer.** The AI work belongs in Python; the caller-facing edge belongs
in TypeScript. The split lets token verification be rejected cheaply, before
anything reaches the service doing ML work.

**Deeper:**
- The gateway does only what an edge should: JWT verification, CORS, body
  limits, a proxy allowlist, error-status mapping.
- It never mints tokens, so authentication stays in one place with one secret to
  rotate.
- Business logic, the AI layer and SQLAlchemy stay in Python, where the
  ecosystem is (`sentence-transformers`, `llama-index`, the Temporal SDK).
- Duplicating logic would be worse than either option alone; the gateway owns no
  domain rules.

**Evidence.** `gateway/src/server.ts`; `gateway/src/auth.ts`; CI job `gateway`
runs typecheck, build and tests.

**Limitation.** There is a second public-path list to keep in sync — the
gateway's and the backend's. That duplication is deliberate (the backend must
answer directly) but it is a real coupling.

**Follow-up.** "Why not put the auth hook in FastAPI middleware?" — Reasonable
and simpler. The gateway exists to keep unauthenticated traffic away from the AI
service entirely.

### Q: Where are the trust boundaries?

**Short answer.** Four, in order: the gateway token check; the backend
authorization; provider output validation; and the output guardrails.

**Deeper:**
- Untrusted ticket text is wrapped in explicit tags, and the system prompt
  states the text is data, not instructions.
- Tool arguments are validated by a Pydantic model with `extra="forbid"`, so a
  hallucinated parameter fails loudly.
- All tools are read-only — the boundary that stops an agent becoming a side
  door around the suggestion-only contract in `docs/spec.md`.
- Output passes guardrails before an agent ever sees it.

**Evidence.** `ai_service.py::_ticket_payload`; `tools/base.py::_StrictArgs`;
`services/guardrails.py`.

---

---

## LangGraph


### Q: How do you prove LangGraph is really executing?

**Short answer.** By asserting on the runtime object, not on an import.

**Deeper:**
- `graph_workflow.py` builds a `StateGraph`, compiles it with a `MemorySaver`,
  and connects `classify → retrieve → draft → confidence_check → approval_gate`.
- `TestLangGraphIsReal` asserts the compiled type, the node set, that a
  checkpointer exists, that state is retrievable by thread id, and that approval
  suspends and resumes.
- A renamed straight-line function fails all of those.

**Evidence.** `tests/test_graph_workflow.py::TestLangGraphIsReal`.

**Limitation.** The checkpointer is in-memory, so checkpoints do not survive a
process restart on this path. `MemorySaver` was chosen to keep the suite offline.

**Follow-up.** "What's the difference between a dependency being installed and
the framework being in the execution path?" — That distinction is the whole story
of Bug 1 below.

### Q: What does `interrupt()` / resume actually do?

**Short answer.** It suspends the graph, persists state, and returns control;
resuming with `Command(resume=...)` replays the node and hands it the decision.

**Deeper:**
- The gate only arms when the caller asks, because the synchronous REST endpoint
  cannot answer mid-run.
---

---

## Temporal


> **Read this before claiming anything.** The workflow is written and tested
> through the local runner, but **the Temporal execution path does not
> currently run.** See "what is actually implemented". Do not describe it as
> production-verified.

### Q: Why Temporal at all?

**Short answer.** Because the part that needs durability is the approval wait,
not the AI call. A human may take hours to decide, and the process can restart
in between.

**Deeper:**
- A synchronous pipeline holds a worker for its whole duration and loses
  everything on restart.
- Temporal's value here is `wait_condition` plus signals: the run suspends with
  no worker held and resumes from recorded history.
- Per-activity timeouts bound each stage independently.

**Evidence.** `workflows/definitions.py` (`wait_condition`, `approve` / `reject`
signals); `workflows/local_runner.py`.

### Q: Temporal versus LangGraph — why have both?

**Short answer.** They solve different problems, and the repository uses them
for different ones.

**Deeper:**
- **LangGraph** models the AI decision process: which stage next, retrieving
  evidence, drafting, and a human-approval interrupt over AI state. In-process,
  checkpointed in memory.
- **Temporal** models durable business-process execution: activities, retries,
  timeouts, and a long-running approval wait that survives a restart.
- Both drive the *same* stage functions from `workflows/pipeline.py`, so they
  cannot disagree about what the pipeline does.
- Both apply the same `needs_approval()` predicate, so a local run and a
  Temporal run cannot disagree about what needs a human.

**Honest caveat.** That separation is the intent and the code reflects it, but
only one path is exercised today. The LangGraph path runs in the suite; the
Temporal path does not (below). Do not present the Temporal half as more
finished than it is.

**Follow-up.** "Would you keep both?" — Here the LangGraph path already covers
the interactive approval case, so Temporal earns its place only when the
approval window is long enough that losing it on restart is unacceptable.

### Q: What is actually implemented, and what is not?

**Short answer.** The workflow definition, its signals, its query, the shared
pipeline and the in-process runner are implemented and tested. All activities
carry `@activity.defn` since `v1.0-interview-verified`, so the two defects
below are FIXED in code — but no run against a live Temporal cluster exists,
so the durable path stays EXPERIMENTAL.

**Deeper — both defects were verified by running the SDK (2026-10-03), then
fixed:**

1. **Activities were not decorated (FIXED).** `temporal_worker.py` used to pass
   plain functions as activities, but `temporalio` requires `@activity.defn`.
   Starting the worker used to fail with:
   `TypeError: Activity activity_classify missing attributes, was it decorated with @activity.defn?`
   All three activities in `definitions.py` are now decorated.

2. **The workflow body was not sandbox-safe (FIXED).** Validating the workflow
   used to raise `RestrictedWorkflowAccessError`, because
   `stage_timeout_seconds()` read `app.config.settings`, whose import touches
   the filesystem. The sandbox-timeout issue was isolated per the
   `v1.0-interview-verified` release notes.

**Status, stated precisely:**
- `IMPLEMENTED_TESTED` — workflow definition, signals, query, shared pipeline,
  in-process runner.
- `LOCAL_RUNTIME_VERIFIED` — the local runner executes the same stages offline.
- `TEMPORAL_EXPERIMENTAL` — code fix verified (`@activity.defn`, sandbox
  isolation) but no live-cluster execution; do not claim a Temporal run.
- `PRODUCTION_VERIFIED` — no.

**Limitation.** Do not say "I ran this on a Temporal cluster." That is not true.

**Follow-up.** "How would you fix it?" — Decorate async activity wrappers around
the sync stages, move the timeout out of `settings` so the workflow body touches
---

---

### Q: How are workflow IDs and retries handled?

**Short answer.** The id defaults to the request id; reuse is
`ALLOW_DUPLICATE_FAILED_ONLY`; activity timeouts come from
`WORKFLOW_STAGE_TIMEOUT_S` (default 120s); there is no explicit `retry_policy`,
so Temporal's default applies.

**Deeper:**
- `ALLOW_DUPLICATE_FAILED_ONLY` means a retry after a *failed* run can start
  again, while a double-submit of a live run is rejected rather than drafting the
  reply twice.
- The local runner implements its own bounded retry (`MAX_STAGE_ATTEMPTS = 3`)
  with a short backoff, because Temporal's automatic retry is unavailable there.
- Keeping the ceiling comparable matters: a failure should surface at a similar
  point in both runtimes when comparing logs.

**Evidence.** `temporal_worker.py::_reuse_policy`; `pipeline.py::run_stage`;
`tests/test_workflows.py::test_permanent_failure_raises_after_the_ceiling`.

**Limitation.** The local runner's retry re-executes the stage from the start; it
is not a resume.

### Q: Activities versus workflows — what's the split?

**Short answer.** Workflow code decides *what happens next* and must be
deterministic. Activities do the non-deterministic work — calling a provider,
reading the database.

**Deeper:**
- Determinism is not stylistic: Temporal replays workflow code on recovery, so a
  workflow that read a clock or an external service directly would produce
  different decisions on replay.
- The three activities are `classify`, `retrieve` and `draft` from
  `pipeline.py`, wrapped for async.
- That split is the design; defect 2 above is exactly what violating it looks
  like — the workflow body reached `Path.resolve()` through a settings read.

**Follow-up.** "What happens if a worker dies mid-activity?" — Temporal retries
the activity on another worker after the heartbeat timeout, up to the retry
policy. This project has not demonstrated that, because the worker does not
start.
- On resume the gate must be re-armed, since LangGraph re-executes the
  interrupting node — a disarmed gate returns `END` and discards the decision.
  That was a real bug found while writing the resume test.
- Stage methods stay public so each is unit-testable alone; graph nodes are thin
  wrappers over them.

**Evidence.** `tests/test_graph_workflow.py::test_approval_gate_suspends_and_resumes`,
`::test_approval_gate_rejection_ends_the_run`.

**Follow-up.** "What does `MemorySaver` own?" — Only checkpointed state keyed by
thread id. Business state lives in Postgres.
domain logic.

---

## Tool calling and MCP


### Q: What is a tool, and what stops arbitrary execution?

**Short answer.** A tool is a typed function the model may request by name. Only
tools explicitly registered in `ToolRegistry` are reachable; anything else
returns "unknown tool".

**Deeper:**
- Each tool declares a Pydantic `args_model`, and the JSON schema sent to the
  model is *derived from that same model* — so the advertised contract and the
  enforced contract cannot drift.
- `extra="forbid"` means an invented parameter is an error rather than silently
  ignored.
- Every tool is read-only. That is deliberate: a mutating tool would be a side
  door around the suggestion-only contract in `docs/spec.md`.
- Every invocation is appended to an audit log with its arguments and outcome.

**Evidence.** `tools/base.py`; `tools/registry.py`;
`tests/test_tools.py::test_tool_rejects_unexpected_arguments`.

**Follow-up.** "What if a tool needs to write?" — It would be a separate,
explicitly-flagged tool class that passes the approval gate, rather than being
callable by the model directly.

### Q: What happens after a tool fails?

**Short answer.** The failure is returned to the model as describable text, the
loop continues, and if the model writes that error into its draft the guardrails
reject the draft.

**Deeper:**
- `ToolRegistry.call` catches `ToolError` and unexpected exceptions, returns a
  `ToolResult` with `ok: false`, and records it.
- The loop renders the failure as a `<tool_result error="true">` element, so the
  model can reason about it rather than seeing a transport error.
- `assert_safe_draft` then blocks internal detail in the draft — the control
  that stops a database error reaching a customer.

**Evidence.** `tests/test_tool_agent.py::test_unknown_tool_is_reported_not_raised`,
`::test_tool_error_cannot_leak_into_the_draft`;
`tests/test_guardrails.py::test_internal_details_are_rejected`.

**Limitation.** The loop has no semantic retry for a failed tool. If the
knowledge base is down, the model gets an error and drafts without that
evidence. A real quality gap, not a crash.

### Q: What is MCP, and why publish these tools?

**Short answer.** MCP is a protocol for exposing tools to external agents.
Publishing means another agent can use this deployment's knowledge base without
importing this codebase.

**Deeper:**
- `app/mcp_server.py` reuses `build_registry` rather than reimplementing the
  tools, so there is one implementation.
- A test asserts MCP's advertised tool set matches the in-process registry, so
---

---

### Q: How do OpenAI and Anthropic tool-call formats differ?

**Short answer.** They do differ, and this repository does **not** yet normalize
them, because no provider is wired to native tool calling.

**Honest answer.** The loop currently uses a deterministic keyword planner
(`KeywordPlanner`) on the offline path. `AnthropicProvider` and
`OpenAIProvider` expose `analyze` and `suggest` only. So: *not implemented, and
here is what it would take* — map each provider's tool-call block into the
`ToolCall(name, arguments)` shape the loop already consumes, and keep that
normalization at the provider boundary.

**Follow-up.** "Why not build it now?" — It needs a real provider key to test
honestly, and a fake parser is worse than none. The loop's contract is already
provider-neutral, so the change is additive.
no filesystem, and mark the activity module as a pass-through import. Then add
one test that starts `WorkflowEnvironment.start_local()` and runs a workflow end
to end — exactly the test whose absence let both defects survive.

## Security


### Q: Explain the gateway authentication bypass you found.

**Short answer.** The public-route allowlist matched on URL path only. Since
`POST /api/tickets` is public, `GET /api/tickets` — the authenticated ticket
list — was also treated as public and proxied with no credentials.

**Deeper:**
- The allowlist is a deliberate product decision: the contact form must work
  before signup.
- The bug was treating path identity as the security property. It is not — the
  same path has different security semantics per method.
- Fix: `isUnauthenticated(url, method)` now requires `method === "POST"` for the
  three public paths.
- Regression tests cover the list, detail and metrics paths.

**Evidence.** `gateway/src/server.ts::isUnauthenticated`;
`gateway/src/server.test.ts::requires auth for GET on the ticket list path`.

**Follow-up.** "What's the general lesson?" — Authorization must consider the
complete request, not a substring of it.

### Q: How do you prevent internal error leakage?

**Short answer.** Three layers: tools never raise into the generator, tool
failures are returned as describable text, and the final draft is checked for
internal detail before anyone sees it.

**Deeper:**
- Tool output is untrusted input. An exception message reaching the model
  context is the same risk as user text reaching it.
- `INTERNAL_DETAIL_PATTERNS` catches database errors, tracebacks, stack traces
  and driver-qualified exceptions.
- The check is on the *output*, so it holds regardless of which layer leaked the
  string in.

---

---

### Q: Can regex stop prompt injection completely?

**Short answer.** No, and I would not claim it does.

**Deeper:**
- The regex is defence in depth: commitment patterns, grounding checks, injection
  markers, internal detail.
- The injection pattern originally required adjacent words, so "ignore **all
  previous** instructions" passed. It now allows filler and covers prompts,
  rules and directions.
- The stronger control is structural: ticket text is tagged as data in the
  prompt, and output is schema-validated and never trusted because it parsed.

**Limitation.** Regex catches known shapes. A novel phrasing will pass, which is
why the pattern list is not the primary control.
  the two cannot drift.
- Transport is stdio, so no port is opened.
- MCP validates argument *types* at the protocol layer; range limits live in the
  tool's own model, and a failure there comes back as a text payload the calling
  agent can reason about.

**Evidence.** `app/mcp_server.py`;
`tests/test_mcp_server.py::test_advertised_tools_match_the_in_process_registry`,
`::test_invalid_arguments_are_rejected_by_the_protocol`.

**Limitation.** There is no MCP *client* here — the tools are published, not
consumed over MCP.

## Guardrails


### Q: What do the guardrails actually check?

**Short answer.** Four things: commitments the AI must not make, facts not
present in the thread, prompt-injection echo, and internal detail.

**Deeper:**
- Commitments: "full refund", "I have processed", percentages of compensation.
- Grounding: a policy or order claim is allowed only if it appears in the
  thread — so a citation the agent already wrote is fine.
- Injection: injection markers echoed into a summary refuse the classification.
- Internal detail: database and traceback text.
- Failure is fail-closed: `reject` returns `502` and leaves the ticket untouched;
  `fallback` substitutes a neutral draft.

**Evidence.** `services/guardrails.py`; `tests/test_guardrails.py`.

**Follow-up.** "Why fail closed rather than drop the sentence?" — Silently
rewriting the model's words would imply the model agreed. Raising is honest and
leaves the decision with the agent.

### Q: What happens if output does not match the expected schema?

**Short answer.** It never reaches persistence. `AnalysisResult` is a Pydantic
model; a parse failure raises `AIProviderError` and the endpoint returns `502`
with the ticket unchanged.

**Deeper:**
- That is why Pydantic sits on both sides: input validation at the API edge, and
  output validation on the provider response.
- The alternative — writing whatever the model returned — is how a malformed
  response silently corrupts a domain record.
- The eval harness counts this as a metric (`schema_validity`), because a model
  that is wrong but valid is a different problem from one that emits garbage.

**Evidence.** `tests/test_ai_service.py::test_malformed_llm_output_502_ticket_unchanged`;
`evaluation/eval_suite.py` schema-validity group.

---

---

## Evaluation


### Q: What does the eval gate test, and what makes it fail?

**Short answer.** Four metric groups on the labeled set, compared against a
recorded baseline. It fails on a >2pp accuracy drop, a guardrail-rate spike
above 0.10, or p95 latency above 2× baseline.

**Deeper:**
- Groups: classification (accuracy, macro-F1, per-category), schema validity,
  guardrail block rate, cost/latency.
- `evaluation/baseline.json` is the reference; `--check` compares and exits
  non-zero.
- CI runs it with the stub provider, so it needs no API key.

**Evidence.** `evaluation/eval_suite.py::check_regression`; `.github/workflows/ci.yml`
step "Eval regression gate".

**Follow-up.** "Why offline?" — A gate that needs a paid key stops being run. The
offline path tests the harness, the schema-validity logic and the gate itself;
provider quality is a separate, opt-in run.

### Q: What can this eval *not* prove?

**Short answer.** Several important things.

- It runs on 92 tickets with a 5-class taxonomy — nothing about another domain.
- The stub's accuracy is not a model's accuracy.
---

---

### Q: Unit tests versus AI evals — what is each for?

**Short answer.** Unit and integration tests prove the code does what I wrote.
Evals measure whether the system output is good. They fail for different reasons
and I would not conflate them.

**Deeper:**
- 331 backend tests are deterministic and offline. They prove behaviour under
  specified conditions.
- The eval measures quality on data, so it can pass while the code is broken
  (the classifier is fine, the endpoint 500s) and fail while the code is correct
  (the model got worse).
- Production monitoring is a third thing: it sees real traffic, which the
  labeled set does not represent.

### Q: How do you stop a model update silently degrading output?

**Short answer.** Pin the model id, keep a labeled set, and gate on it. The eval
gate is that mechanism; `--check` exists so a model string change cannot land
without someone looking at the metrics.

**Limitation.** The labeled set is 92 tickets. It catches a gross regression, not
a subtle one. A real system would track production traffic quality as well,
which needs labels this repository does not have.
**Evidence.** `guardrails.py::INTERNAL_DETAIL_PATTERNS`;
`tests/test_guardrails.py::test_internal_details_are_rejected`.

**Limitation.** It is a pattern list. A sufficiently unusual error string could
pass. The structural control is that tools are read-only and errors are
summarised — not the regex.

## Observability


### Q: What span data do you record?

**Short answer.** One span per LLM call, with the GenAI semantic attributes:
system, request model, operation name, input/output token counts — plus a custom
cost attribute, since that is the number an on-call engineer needs.

**Evidence.** `services/telemetry.py`;
`tests/test_telemetry.py::test_semconv_attribute_names_are_stable`.

**Follow-up.** "Why pin the attribute names in a test?" — Renaming
`gen_ai.usage.input_tokens` silently breaks every downstream dashboard. The test
makes that rename a failure rather than a surprise.

### Q: Why durable `ai_call_traces` if OpenTelemetry already exists?

**Short answer.** They answer different questions. OTel is a trace view of one
request; the table answers an aggregate question about a week of traffic.

**Deeper:**
- The table survives a restart and can group by model, operation and ticket —
  which in-process counters cannot, and which OTLP only answers if a collector
  is configured.
- Tracing is optional and off by default; the table is always on.
- The table stores an outcome and an error kind, a product concern rather than a
  tracing concern.

**Evidence.** `services/tracing.py`; `tests/test_tracing.py`.

**Follow-up.** "What is not implemented?" — **Langfuse is not implemented.**
`telemetry.py` emits OTel GenAI spans, which Langfuse ingests over OTLP, so
pointing `OTEL_EXPORTER_OTLP_ENDPOINT` at Langfuse should work — but no hosted
Langfuse project is configured and none has been run against. Say
"OpenTelemetry-compatible instrumentation", not "we use Langfuse".

### Q: What does a 502 from the backend mean, and why not a 503?

**Short answer.** The backend answers `502` when an AI provider fails. The
gateway is fine and the request was valid, so it passes it through. Only a
transport failure is a `503`.

**Evidence.** `gateway/src/backend.ts::mapStatus`;
`gateway/src/backend.test.ts::test_keeps_a_backend_502_as_502`,
`::test_returns_503_when_the_backend_is_unreachable`.

---

---

## Cost and latency


### Q: What is measured and what is projected?

**Short answer.** Token counts are measured. Prices are the vendors' published
rates applied to those counts. Nobody has run 10,000 real tickets through Claude
here, so calling the per-model figures a measurement would be false.

**Deeper:**
- Measured: tokens per ticket (147.8 on the 92-ticket set), schema validity,
  guardrail rate, and the latency of the **offline stub path**.
- Projected: the per-model cost table, computed as published rate × measured
  tokens.
- The stub has no published price, so it reports *unpriced* rather than $0.00.
  `cost_usd` returns `None`, the column is nullable, and `summarize()` reports
  `total_cost_usd: null` when any call is unpriced.

**Evidence.** `docs/COST-LATENCY.md`;
`tests/test_tracing.py::test_unpriced_model_records_null_cost_not_zero`;
`tests/test_eval_suite.py::test_unpriced_model_reports_gap_not_zero`.

### Q: What exactly does the 0.66 ms p50 measure?

**Short answer.** The offline stub path — a deterministic rule-based classifier
with no network call. It measures the harness and the Python code around it. It
is **not** an LLM latency figure, and I would not present it as one.

**Deeper:**
- A real provider call adds network round-trip time plus queueing, typically
---

---

### Q: Why isn't the estimated cost equal to the bill?

**Short answer.** Because the estimate uses published list prices and measured
token counts, while a bill reflects negotiated rates, batch discounts, cached
prompt tokens, and any token the provider counted differently.

**Deeper:**
- The rate table is versioned (`PRICING_TABLE_VERSION`) so a figure is only
  meaningful next to the table it came from.
- Cached prompt tokens are billed far below input tokens; the estimate does not
  model caching.
- To reconcile, you compare the trace rows against the invoice — which is a
  concrete next step, not something this project has done.

### Q: What is p50, and why is it useful?

**Short answer.** The median latency: half of calls are faster, half slower. It
resists the outliers that make an average misleading.

**Deeper:**
- p95 answers "how bad is it for the unlucky 5%", which is what a user
  complaint usually reflects.
- Averages are close to useless here, because one 30-second timeout drags the
  mean past the point where most calls sit.
- Both are reported per model, because a slow model is a product decision, not
  an incident.

**Follow-up.** "What would you add at scale?" — p99, and per-operation splits,
since retrieval-heavy turns have a different profile from single-pass
classification.
- Guardrail block rate on the stub is not a real model's block rate.
- It cannot prove safety; it proves measurable drift on a fixed set.
- It does not catch a rare failure the dataset does not contain.

**Limitation.** The `1.0` figures in `docs/model-comparison.md` come from a
19-sample validation split. They are dataset-specific, not benchmark results.

## Embeddings and retrieval


### Q: Why store the model and dimension beside each vector?

**Short answer.** Because vectors from different embedding models are not
comparable. Cosine similarity assumes both vectors live in the same space.

**Deeper:**
- Two unrelated models produce different vector spaces; the numeric cosine
  between them is meaningless, not merely inaccurate.
- Storing the producer and width lets the system detect the mismatch and
  recompute instead of returning a confidently wrong similarity score.
- This was added after the embedding bypass was found (Bug 2).

**Evidence.** `models/ai.py::TicketEmbedding`;
`tests/test_retrieval.py::test_stale_embedding_is_recomputed_on_provider_switch`.

**Follow-up.** "What happens after switching models?" — Rows whose stored
producer differs from the active one are recomputed on next access. That is a
lazy backfill; a real migration would re-index eagerly, since the current
approach re-embeds one ticket per request that touches it.

### Q: Why Qdrant, and what is stored?

**Short answer.** Qdrant is the store a deployment scales to; the in-process
LlamaIndex index stays the default because it needs no service.

**Deeper:**
- Documents are chunked (512/50 overlap) with a content-derived stable id, so
  re-ingesting unchanged content upserts rather than duplicating points.
- Payload carries the chunk text, its source, and file metadata.
- Reachable three ways: `QDRANT_URL`, `QDRANT_PATH` (embedded, on-disk), then
  `:memory:`.
- If none work, retrieval falls back to LlamaIndex. Degraded retrieval beats a
  broken feature.

**Evidence.** `services/vector_store.py`; `tests/test_vector_store.py`.

**Limitation.** The knowledge base is two documents. Retrieval works end to end
but is not stressed.

---

---

## The TypeScript gateway


### Q: What does `noUncheckedIndexedAccess` actually prevent?

**Short answer.** It types `arr[i]` as `T | undefined` instead of `T`, forcing a
check before use.

**Deeper:**
- Without it, `parts[i].content` type-checks as `string` even when `parts` is
  empty, and the bug surfaces as a runtime `undefined` deep inside a request.
- It found real issues while writing the gateway: the query-string split needed
  `?? ''` because the index access is genuinely optional.

**Evidence.** `gateway/tsconfig.json`;
`gateway/src/server.ts` (`request.url.split('?')[1] ?? ''`).

**Follow-up.** "Is it worth the friction?" — Yes for a component whose whole job
is parsing untrusted input.

### Q: What does `exactOptionalPropertyTypes` change?

---

---

### Q: What is strict TypeScript buying here, concretely?

**Short answer.** The proxy boundary is where untrusted data enters. Strict TS
plus those two flags means a malformed request object cannot reach the backend
client without a compile error.

---

---

## Docker and Compose


### Q: What starts in Compose, and what does `docker compose config` prove?

**Short answer.** Postgres, the backend, the gateway, the frontend, plus optional
Qdrant and Temporal.

**Deeper:**
- `docker compose config` proves the file is valid and interpolates correctly.
  Notably `JWT_SECRET` has no default: compose fails fast rather than booting
  with a known signing key. That behaviour is only visible when you run it.
- Qdrant, Temporal and the gateway are each optional in practice — the app falls
  back to LlamaIndex, the in-process runner, and direct backend access.
- `depends_on` with `condition: service_healthy` orders startup.

**Limitation.** A valid config proves nothing about the services starting,
building, or reaching each other. I have not run `docker compose up` against this
tree.

**Follow-up.** "What differs in production?" — Managed Postgres, Render for the
backend, Cloudflare Pages for the frontend; secrets from the platform rather
than the environment.
  hundreds of milliseconds to seconds.
- That is why latency is recorded per call in `ai_call_traces` and reported per
  model, rather than as one project-wide number.
- To get real numbers I would set a provider key, run `eval_suite.py --json`,
  and read `latency_ms` from the resulting traces.

**Limitation.** The headline latency in the docs is a floor, not a
representative value.

## Testing strategy


### Q: "331 tests" means what, exactly?

**Short answer.** It means the specified behaviours are exercised, offline, with
no API key — and it does **not** mean the system is production-ready.

**Deeper — what the tests actually cover:**
- Unit: pricing arithmetic, percentile maths, state-machine transitions, the
  chunking id function.
- Integration: the real HTTP surface against a real database — auth,
  authorization, boundaries, concurrency.
- Provider: each backend's wire protocol, with a fake HTTP transport.
- Security: guardrail failure modes, role escalation, token rejection.
- Gateway: proxying, status mapping, auth.
- Eval: the harness's own arithmetic and its regression gate.

**Limitations the count hides:**
- 331 tests can all pass while the system is unusable — no test proves the UI is
  good or that the prompt strategy is right.
- The stub path is exercised far more than any real provider.
- Nothing proves the Temporal path runs; indeed, as above, it does not.

**Follow-up.** "What would you add first?" — A test that starts a real Temporal
server and runs a workflow end to end. That is the gap that hid two defects.

### Q: How did you find the bugs you did?

**Short answer.** Mostly by writing a test that asserted the *opposite* of the
claim. A test that only confirms what already works cannot catch a
misrepresentation.

**Deeper:**
- "Assert LangGraph is a `CompiledStateGraph`" fails if it is not.
- "Assert `ensure_embedding` returns 384-dim with `AI_EMBED_PROVIDER=hf`" fails
  if the setting is bypassed.
- "Assert `GET /api/tickets` returns 401" fails if the allowlist is
  path-only.
- "Assert a draft containing `no such table` is blocked" fails if the guardrail
  is missing.

**Limitation.** This works when I think to write the adversarial assertion. It
did not work for the Temporal worker, because every test used the local runner.

---

---

## Bugs I can explain in an interview


### Bug 1 — the framework was installed but never executed

**Symptom.** The project described itself as using LangGraph, and a dependency
was installed.

**Root cause.** Documentation and architecture ran ahead of the execution path.
`process_ticket` was four method calls in sequence; nothing imported
`langgraph`.

**Detection.** A test asserting the compiled app is a `CompiledStateGraph` with
the expected node set. An import-presence check would have passed.

**Lesson.** *Dependency presence is not runtime use.* "It is in
`requirements.txt`" proves nothing; you have to assert on the runtime object.

### Bug 2 — a configuration setting that changed nothing

**Symptom.** `AI_EMBED_PROVIDER=hf` had no effect. `embed_text` returned a
384-dim MiniLM vector while everything persisted was still 128-dim.

**Root cause.** `ensure_embedding` called the bag-of-words embedder directly,
bypassing provider selection. The only call site that honoured the setting was
not the one that mattered.

**Detection.** The existing test asserted `len in (128, 384)` — loose enough to
pass while the defect was live. Tightening it to an exact width made it fail.

**Lesson.** *Configuration without execution-path verification is meaningless,*
and so is a test loose enough to pass either way.

**Extra fix.** Vectors now record their producer and width, and mismatched rows
are recomputed — because comparing embeddings from different models is
meaningless, not merely inaccurate.

### Bug 3 — authorization matched only the path

**Symptom.** `GET /api/tickets`, the authenticated ticket list, was proxied to
the backend without credentials.

**Root cause.** The public-route allowlist matched URL path only and ignored the
HTTP method. The same path is public for `POST` and private for `GET`.

**Detection.** Three gateway tests asserting 401 on the list, detail and metrics
paths.

**Lesson.** *Authorization belongs to the complete request.* A substring of the
request is not a security property.

### Bug 4 — an internal error could reach a customer

**Symptom.** `OperationalError: no such table: tickets` could appear in a draft.

**Root cause.** The agent loop reports a failed tool call to the model as text.
That text is untrusted input like any other, and it was flowing into the
customer-facing draft.

**Detection.** A test where the provider echoes the tool error into its draft,
asserting the draft is withheld.

**Lesson.** *Tool output is untrusted input too.* The error path is the one
people forget to sanitize.

**Fix.** `INTERNAL_DETAIL_PATTERNS` on the output, plus read-only tools so the
error surface is smaller to begin with.

### Bug 5 — a guardrail regex with a gap in the middle

**Symptom.** "Ignore all previous instructions" — the most common phrasing —
passed the injection check.

**Root cause.** The pattern required the words adjacent: `ignore` + `previous` +
`instructions`. Real attempts insert "all" in the middle.

**Detection.** A parametrized test over the common phrasings.

**Lesson.** *A regex guardrail is defence in depth, not prompt-injection
security.* Widening the pattern helps; the structural controls — untrusted-data
tagging, output validation — are what actually bound the risk.
**Short answer.** It distinguishes "property absent" from "property present and
`undefined`", which are different values.

**Deeper:**
- Without it, `{ token: undefined }` is assignable to `{ token?: string }`, so
  an optional field silently becomes explicitly-undefined.
- That bit immediately: `RequestInit` rejects `body: undefined`, and the backend
  client's options object could not accept a possibly-undefined token.
- The fix was to build the init object and assign `body` only when it exists.

**Evidence.** `gateway/src/backend.ts` (the `init: RequestInit` construction);
`gateway/tsconfig.json`.

## What I deliberately do NOT claim


Stating these is not weakness; claiming something false and being caught is.

- **Redis is not used.** Rate limiting is in-process, so it is per-instance and
  under-counts behind more than one worker. The per-IP limit on guest submissions
  is the one that matters and it would need a shared store.
- **Temporal does not run.** The workflow definition, signals, query, shared
  pipeline and local runner are implemented and tested. The Temporal worker
  cannot start — missing `@activity.defn`, and a workflow body that is not
  sandbox-safe. It has never run against a cluster.
- **Langfuse is not implemented.** `telemetry.py` emits OTel GenAI spans, which
  Langfuse ingests over OTLP. That is compatibility, not integration, and no
  hosted Langfuse has been run against.
- **No production traffic.** "Implemented, tested and deployable" is accurate;
  "shipped to N users" is not.
- **Cost figures are estimated**, from published rates applied to measured
  tokens. No provider invoice has been reconciled.
- **The 0.66 ms p50 is the offline stub path**, not an LLM latency figure.
- **The `1.0` eval scores come from a 19-sample validation split** drawn from 92
  labels. They are dataset-specific.
- **Native provider tool calling is not implemented.** The loop uses a
  deterministic planner offline; provider tool-call formats are not normalized
  yet.
- **The knowledge base is two documents.** RAG works end to end but is not
  stressed.
- **No AWS.** Deployment target is Render plus Cloudflare Pages.
- **No multi-tenancy, no SLA engine, no fine-grained RBAC** beyond
  customer/agent.
- **`docker compose up` has not been run against this tree.** The config is
  valid; the stack starting is not proven here.

---

---

## Multi-provider LLM


### Q: Why support several providers?

**Short answer.** Provider-neutral where it is cheap, provider-specific where it
is not.

**Deeper:**
- Selection is by `AI_PROVIDER`; `get_provider()` builds the implementation once.
- Provider-neutral: the `AnalysisProvider` protocol, the validated output model,
  the guardrail pass, the trace, and the cost computation. None of these know
  which provider ran.
- Provider-specific: the request shape, the auth header, and how usage is
  reported. OpenAI returns `prompt_tokens`/`completion_tokens`; Anthropic returns
  `input_tokens`/`output_tokens`. Each parses its own and normalizes to
  `TokenUsage`.
- When a provider reports no usage at all, the count is marked `estimated`
  rather than silently becoming zero.

**Evidence.** `ai_service.py::_anthropic_usage`, `OpenAIProvider._chat`;
`tests/test_ai_providers.py`;
`tests/test_ai_usage.py::test_openai_missing_usage_falls_back_to_estimate`.

**Limitation.** There is no automatic provider failover. If Anthropic fails the
call fails; the retry only covers transient errors from the same provider.

### Q: What happens when a provider is unavailable?

**Short answer.** A transient failure is retried once; a persistent one returns
`502`, leaves the ticket untouched, and the failure is recorded in
`ai_call_traces` with its error kind.

**Evidence.** `ai_service._call_with_retry`;
`tests/test_ai_service.py::test_retry_exhausted_raises_502`.

---

---

## Failure scenarios


| Scenario | What happens | Evidence |
|---|---|---|
| Provider unavailable | Retry once on transient, else `502`, ticket untouched, trace records the error | `test_retry_exhausted_raises_502` |
| Malformed provider output | Schema validation fails, nothing persisted | `test_malformed_llm_output_502_ticket_unchanged` |
| Guardrail violation | `502`, draft withheld, no message created | `test_agent_endpoint_502_when_the_draft_is_blocked` |
| Tool throws | Error returned to the loop, model continues, draft checked | `test_unknown_tool_is_reported_not_raised` |
| Tool error echoed into a draft | Guardrail blocks it | `test_tool_error_cannot_leak_into_the_draft` |
| Qdrant unreachable | Falls back to LlamaIndex | `test_knowledge_base_falls_back_when_qdrant_raises` |
| Knowledge base empty | Keyword retrieval returns `[]`, pipeline continues | `test_ticket_history_tool_on_missing_ticket` |
| Agent loops | Repeated identical calls detected; `TOOL_MAX_STEPS` ceiling | `test_repeated_tool_call_is_stopped`, `test_step_ceiling_is_enforced` |
| **Temporal worker starts** | **Fails — activities are not decorated and the workflow body is not sandbox-safe** | verified by running the SDK; see Temporal section |
| Gateway down | Backend still serves directly | Compose topology |

---

---

## Trade-offs


### Q: What would you redesign with more time?

**Short answer.** Three things, in order.

- **Fix the Temporal path and test it against a real server.** Highest-value gap:
  it is a durable-workflow story I currently cannot demonstrate.
- **Native provider tool calling.** The loop's contract is provider-neutral
  already; the missing piece is mapping each provider's tool-call block into it.
- **Shared rate limiting.** In-process buckets are wrong behind more than one
  worker, and that is the limit protecting a public endpoint.

### Q: What is still not production-proven?

**Short answer.** Everything that needs real traffic: latency, cost, guardrail
behaviour under a real model, retrieval quality — and the Temporal path.

### Q: What is the biggest bug you found?

**Short answer.** The gateway auth bypass, because it was a live security defect
rather than an inaccuracy — and because it was found by one line of test,
`expect(res.statusCode).toBe(401)`, which is the cheapest possible check.

**Follow-up.** "What did you change afterwards?" — I started asserting the
opposite of every claim: for each thing the README said was true, a test that
fails if it is not. Two of the five bugs came from exactly that habit.
> Không đáng tin *như một benchmark* — và tôi là người đầu tiên nói điều đó. Val chỉ có 19 samples, TF-IDF cũng đạt 1.0 trên cùng split, tức là dataset-specific. Giá trị thật của bảng số này là **quy trình**: cùng 92 tickets, cùng split, cùng metric — đo cả stub, TF-IDF, DistilBERT fine-tune, và LLM 8B, rồi báo cáo cả con số xấu (79% của LLM) một cách công khai. Tôi muốn được đánh giá ở việc "biết cách đo và không tự lừa mình bằng perfect score", không phải ở con số 1.0.

---
---

## The model fine-tune story

Kept because it is still part of the project, and because the honest framing of
a weak number is itself an answer. Facts here are reproducible from
`evaluation/train_transformer.py`, `evaluation/tickets.json` (92 labeled
tickets) and `docs/model-comparison.md`.

### Q: "Bạn fine-tune model ở đâu? Có thật không?"

> Fine-tune trên máy local — DistilBERT-base-uncased, classification head 5 lớp,
> trên chính dataset 92 labeled tickets của project. Split stratified theo
> category với seed 42: 73 train / 19 val. Train 3 epochs (30 steps, batch 8) trên
> Apple M1 — MPS, khoảng 8 giây. Kết quả: accuracy 1.0 và macro-F1 1.0 trên val
> split. Script nằm ở `evaluation/train_transformer.py`, artifact gitignored, và
> `pytest` có test chạy lại khi artifact tồn tại — nên con số này không phải
> claim, nó verify lại được.

**Nếu bị hỏi sâu:**
- Loss trung bình qua 3 epochs: ~1.17 (chance level cho 5 lớp là ln 5 ≈ 1.61).
- Không có hyperparameter tuning — đúng nghĩa "minimal fine-tune", dùng lr
  default của Trainer.
- Artifact không commit vì trọng số (268MB) không thuộc git; fresh checkout vẫn
  reproduce được bằng 1 lệnh.

### Q: "Sao không fine-tune qua API (OpenAI, Cloudflare, Together)?"

> Ba lý do, theo thứ tự quyết định:
> 1. **Cloudflare Workers AI không có train-via-API.** Cái gọi là "finetune" của
>    họ thực chất là *serving LoRA*: bạn train adapter ở nơi khác, upload
>    `adapter_model.safetensors` + `adapter_config.json`, rồi inference kèm
>    `lora:<id>`. Không phải "bấm API là train".
> 2. **92 labels không đáng chi phí.** Together/Fireworks có LoRA-via-API thật,
>    nhưng để host một endpoint cho classifier 5 lớp với 92 samples là
>    overkill — và TF-IDF + LogReg đã đạt 1.0 rồi.
> 3. **OpenAI fine-tune đang wind-down với user mới.** Không chọn path có rủi ro
>    ngừng hỗ trợ cho một project portfolio.
>
> Fine-tune DistilBERT local mất 14 giây, 0 đồng, và cho tôi trải nghiệm train
> thật để nói chuyện — đó là lựa chọn đúng.

### Q: "DistilBERT và TF-IDF đều 1.0 — vậy tại sao LLM 8B chỉ 79%?"

> Ba con số đó nói đúng điều tôi muốn chứng minh: **taxonomy này hẹp** (5 lớp cố
> định, ticket pattern ổn định) nên supervised nhỏ đã giải quyết trọn. Llama
> 3.1 8B qua prompt đạt 79.3% / 0.78 — nhầm lẫn tập trung ở cặp refund↔payment
> vì hai loại dùng từ ngữ gần nhau. Bài học (nói được thành câu): *khi có labeled
> data, classifier nhỏ supervised thắng LLM-thông-dịch prompt-based trên tác vụ
> hẹp; LLM hợp lý ở chỗ không cần labels và xử lý open-ended.*

### Q: "LLM trong prod là gì — tại sao không dùng fine-tuned model trong API?"

> Prod chạy provider qua `AI_PROVIDER` (stub offline + OpenAI + Anthropic +
> Cloudflare). Fine-tuned DistilBERT nằm ở *evaluation layer*, không phải serving
> layer — đây là quyết định kiến trúc:
> - LLM làm được nhiều việc classifier 5 lớp không làm: **summary** và **draft
>   reply** (có grounding vào ticket + policy).
> - Toàn bộ AI là **suggestion-only**: output schema-validated, guardrail
>   fail-closed. Agent vẫn là người gửi và quyết định.
> - `ai_predictions` log model + confidence, nên mọi prediction đều audit được.

### Q: "Con số 1.0 có đáng tin không?" (câu hỏi phản biện quan trọng nhất)

> Không đáng tin *như một benchmark* — và tôi là người đầu tiên nói điều đó. Val
> chỉ có 19 samples, TF-IDF cũng đạt 1.0 trên cùng split, tức là
> dataset-specific. Giá trị thật của bảng số này là **quy trình**: cùng 92
> tickets, cùng split, cùng metric — đo cả stub, TF-IDF, DistilBERT fine-tune
> và LLM 8B, rồi báo cáo cả con số xấu (79% của LLM) một cách công khai. Tôi
> muốn được đánh giá ở việc "biết cách đo và không tự lừa mình bằng perfect
> score", không phải ở con số 1.0.---

## 60-second project pitch

> SupportDesk is a support ticket system where AI assists agents and never
> decides — it triages, retrieves policy evidence, calls read-only tools and
> drafts a reply, and it never sends anything or promises a refund on its own
> authority.
>
> Three services: a React SPA, a TypeScript gateway that verifies every JWT
> before a request reaches the AI service, and a Python backend.
>
> The AI work is orchestrated two ways: a LangGraph pipeline with checkpointing
> and an approval interrupt, and a Temporal workflow definition with the same
> stages for durable execution — though I should be clear that the Temporal
> worker doesn't currently start, so the LangGraph path is the one I can
> demonstrate.
>
> Around that: a tool-calling loop with validated schemas and a step ceiling, the
> same tools published over MCP, Qdrant for retrieval with a fallback,
> deterministic guardrails that fail closed, per-call cost and latency tracing,
> and an evaluation harness wired into CI as a regression gate.
>
> The interesting part is the last six months: my own audit flagged three false
> claims, and fixing them in code — plus two real defects the new tests caught —
> is most of the work.

---

## 5-minute deep dive — outline

Not a script. Have the order; the details come from the sections above.

1. **Problem** — an LLM in a support workflow with no measurement, no cost
   visibility, no trust boundary, and no durability.
2. **Architecture** — React → Node gateway → Python AI service; why the split;
   where trust boundaries sit.
3. **Request flow** — one ticket from submission to a guarded draft, with the
   trace written on every path.
4. **Workflow design** — LangGraph for AI decisions, Temporal for durable
   process; the shared stage functions; the approval gate.
5. **AI and tool safety** — read-only tools, validated schemas, three guardrail
   layers, fail-closed behaviour.
6. **Observability and evaluation** — OTel spans plus durable traces; the
   four-metric eval and its regression gate.
7. **Bug story** — pick one. The gateway auth bypass is the strongest: a live
   security defect found by a single line of test.
8. **Trade-off and limitation** — Temporal does not run; Redis is not used;
   nothing is production-proven.

If they ask "what would you do next", the answer is item 4 — and it is the same
answer whether or not they press on it.