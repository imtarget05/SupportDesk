"""Durable per-call tracing for LLM usage.

Every provider call is written to ``ai_call_traces`` so cost, latency and
failure rate survive a restart and can be broken down by model, operation and
ticket — which is what an on-call engineer needs and what the in-process
counters in ``metrics.py`` cannot provide.

Recording is best-effort by design: tracing must never turn a successful AI call
into a failed request, so write errors are logged and swallowed.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import AICallTrace
from app.services import pricing, telemetry

logger = logging.getLogger(__name__)

# Outcome vocabulary, kept small on purpose so the column is actually groupable.
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_BLOCKED = "blocked"  # guardrail rejected the output


def new_request_id() -> str:
    """Correlate every LLM call made while serving one request."""
    return uuid.uuid4().hex[:16]


@contextmanager
def trace_call(
    operation: str,
    provider: str,
    model: str,
    ticket_id: int | None = None,
    session: Session | None = None,
) -> Iterator[dict[str, Any]]:
    """Time one provider call and persist its trace.

    Usage:
        with trace_call("triage", "stub", "stub", ticket_id) as t:
            result = provider.analyze(...)
            t["usage"] = provider.last_usage
            t["confidence"] = result.confidence

    The trace is written on exit whatever happens, so failures are recorded too.
    """
    record: dict[str, Any] = {
        "request_id": new_request_id(),
        "operation": operation,
        "provider": provider,
        "model": model,
        "ticket_id": ticket_id,
        "usage": None,
        "confidence": None,
        "outcome": OUTCOME_OK,
        "error_kind": None,
    }
    started = time.perf_counter()
    with telemetry.llm_span(operation, model, provider) as span_attrs:
        try:
            yield record
        except Exception as exc:  # noqa: BLE001 — classify, then re-raise unchanged
            record["outcome"] = OUTCOME_ERROR
            record["error_kind"] = type(exc).__name__
            raise
        finally:
            record["span_attrs"] = span_attrs
            latency_ms = int((time.perf_counter() - started) * 1000)
            try:
                _persist(record, latency_ms, session)
            except Exception:  # noqa: BLE001 — tracing must not break the request
                logger.warning("Failed to persist AI call trace", exc_info=True)


def mark_blocked(record: dict[str, Any]) -> None:
    """Flag a trace whose output was rejected by a guardrail."""
    record["outcome"] = OUTCOME_BLOCKED
    record["error_kind"] = "GuardrailError"


def _persist(record: dict[str, Any], latency_ms: int, session: Session | None) -> None:
    usage = record.get("usage")
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    cost = pricing.cost_usd(record["model"], prompt_tokens, completion_tokens)
    telemetry.set_usage(record.get("span_attrs") or {}, record["model"], usage)

    row = AICallTrace(
        request_id=record["request_id"],
        ticket_id=record["ticket_id"],
        operation=record["operation"],
        provider=record["provider"],
        model=record["model"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        pricing_table_version=pricing.PRICING_TABLE_VERSION if cost is not None else None,
        usage_estimated=bool(getattr(usage, "estimated", False)),
        latency_ms=latency_ms,
        outcome=record["outcome"],
        error_kind=record["error_kind"],
        confidence=record.get("confidence"),
    )
    if session is not None:
        session.add(row)
        return
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
    finally:
        db.close()


def _percentile(sorted_values: list[int], pct: float) -> int:
    if not sorted_values:
        return 0
    index = min(
        len(sorted_values) - 1, int(round((pct / 100) * len(sorted_values) + 0.5)) - 1
    )
    return int(sorted_values[max(0, index)])


def summarize(db: Session, limit: int = 50) -> dict[str, Any]:
    """Aggregate traces by model for the metrics API.

    ``total_cost_usd`` is ``None`` when at least one call could not be priced,
    rather than reporting a partial sum as if it were the complete figure.
    """
    rows = db.query(AICallTrace).all()
    by_model: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_model.setdefault(
            row.model,
            {
                "model": row.model,
                "provider": row.provider,
                "calls": 0,
                "errors": 0,
                "blocked": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "unpriced_calls": 0,
                "latencies": [],
            },
        )
        entry["calls"] += 1
        if row.outcome == OUTCOME_ERROR:
            entry["errors"] += 1
        elif row.outcome == OUTCOME_BLOCKED:
            entry["blocked"] += 1
        entry["prompt_tokens"] += row.prompt_tokens
        entry["completion_tokens"] += row.completion_tokens
        if row.cost_usd is None:
            entry["unpriced_calls"] += 1
        else:
            entry["cost_usd"] += row.cost_usd
        entry["latencies"].append(row.latency_ms)

    models = []
    for entry in by_model.values():
        latencies = sorted(entry.pop("latencies"))
        successful = entry["calls"] - entry["errors"] - entry["blocked"]
        models.append(
            {
                **entry,
                "cost_usd": round(entry["cost_usd"], 6),
                "p50_latency_ms": _percentile(latencies, 50),
                "p95_latency_ms": _percentile(latencies, 95),
                "cost_per_successful_call_usd": (
                    round(entry["cost_usd"] / successful, 8)
                    if entry["unpriced_calls"] == 0 and successful
                    else None
                ),
            }
        )
    models.sort(key=lambda m: m["calls"], reverse=True)

    unpriced = sum(m["unpriced_calls"] for m in models)
    return {
        "total_calls": len(rows),
        "total_errors": sum(1 for r in rows if r.outcome == OUTCOME_ERROR),
        "total_blocked": sum(1 for r in rows if r.outcome == OUTCOME_BLOCKED),
        "total_cost_usd": None
        if unpriced
        else round(sum(m["cost_usd"] for m in models), 6),
        "unpriced_calls": unpriced,
        "pricing_table_version": pricing.PRICING_TABLE_VERSION,
        "pricing_as_of": pricing.PRICING_AS_OF,
        "by_model": models[:limit],
    }
