# Cost & Latency — measured, with the assumptions stated

Every number here was produced by `evaluation/eval_suite.py` on the 92-ticket
labeled set, or computed from the same rate table the application uses at
runtime (`app/services/pricing.py`, version `2026-10-10`).

Reproduce:

```bash
AI_PROVIDER=stub python evaluation/eval_suite.py --json
```

---

## Measured: offline stub provider

| Metric | Value |
|---|---|
| Tickets evaluated | 92 |
| Accuracy / macro-F1 | 93.5% / 0.94 |
| Schema validity | 92/92 (100%) |
| Guardrail block rate | 20/92 (21.7%) |
| Provider errors | 0 |
| Tokens per ticket | 147.8 (13,600 total) |
| Latency p50 / p95 / max | 0.66 ms / 0.84 ms / 7.17 ms |
| Cost per ticket | **not priced** — `stub` is absent from the rate table |

The stub is a deterministic rule-based classifier: it makes no network call, so
its latency measures the harness rather than any model. It is a floor, not a
performance claim.

Cost is reported as *unpriced* rather than `$0.00` on purpose. A missing price is
a different fact from a free call, and the code keeps them distinct: see
`pricing.cost_usd()` returning `None`, `ai_call_traces.cost_usd` being nullable,
and `tracing.summarize()` reporting `total_cost_usd: null` when any call is
unpriced.

## Projected: priced models

Token counts above are real (the stub reports its own usage). Applying the
published per-token rates to those counts gives the per-ticket cost a real model
would incur at the same volume. Two token splits are shown, because the actual
split depends on the operation:

- **triage + draft** ≈ 100 prompt / 48 completion tokens per ticket
- **triage only** ≈ 120 prompt / 28 completion tokens per ticket

| Model | Triage + draft | Triage only |
|---|---|---|
| `@cf/meta/llama-3.1-8b-instruct` (Workers AI) | $0.000030 | $0.000030 |
| `gpt-4o-mini` | $0.000044 | $0.000035 |
| `gpt-4.1-mini` | $0.000117 | $0.000093 |
| `claude-haiku-4-5` | $0.000340 | $0.000260 |
| `claude-sonnet-4-5` | $0.001020 | $0.000780 |

At 10,000 tickets/month, `gpt-4o-mini` costs roughly **$0.44** for triage+draft;
`claude-sonnet-4-5` roughly **$10.20**. The token counts scale with ticket
length, so a support desk with long threads will see proportionally more.

### Why these are projections, not measurements

The token counts are measured. The rates are the vendors' published prices as of
`2026-10-10`. Nobody has run 10,000 real tickets through `claude-sonnet-4-5`
here, so calling the right-hand column a measurement would be false. What *is*
measured is the pipeline this project uses to answer the question for real: with
a provider key set, every call lands in `ai_call_traces` with actual token
counts from the provider's own response, and `GET /api/metrics/ai` returns
per-model p50/p95, error rate and cost per successful call.

## What actually moves the number

Reading the table alone invites the wrong conclusion — the differences look small
in absolute terms, and they are. The larger cost risks are structural:

- **Retries.** `ai_service._call_with_retry()` retries transient failures once.
  A provider at 99.5% success on a 10k/month feature costs roughly twice the
  naive figure, because half the failures become two calls.
- **The agent loop.** `POST /api/tickets/{id}/ai/agent` can make several model
  turns per ticket. `TOOL_MAX_STEPS` bounds it, and repeated identical tool calls
  are detected and stopped — but a loop that does run will show up immediately
  as an abnormal call count per ticket in `/api/metrics/ai`.
- **Retrieval.** Each turn that retrieves evidence pays for the larger prompt.
  This is why `/api/metrics/ai` breaks cost down per operation rather than
  reporting one blended number.

## Reproducing a cost figure yourself

```bash
# Offline: confirms the harness, token counts, and the unpriced path
AI_PROVIDER=stub python evaluation/eval_suite.py

# With a provider: real usage counters feed the same report
AI_PROVIDER=openai OPENAI_API_KEY=... python evaluation/eval_suite.py --json

# Whatever ran, the traces are in the DB — grouped per model
# GET /api/metrics/ai          (agent-only)
# GET /api/metrics/ai/recent   (agent-only)
```