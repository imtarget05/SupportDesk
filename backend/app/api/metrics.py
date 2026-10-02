"""Metrics API endpoint — delegates to app.services.metrics for state.

This file is a thin FastAPI wrapper only. The actual counters live in
``app.services.metrics`` so ai_service can call record_call/record_error
without a circular import through the API layer.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_agent
from app.services import metrics as _metrics
from app.services import tracing

router = APIRouter(tags=["metrics"])  # prefix added by include_router in main.py


# Operational counters only — agent-scoped. It used to be anonymous and
# hidden from the schema, which made an unauthenticated capability invisible
# to anyone reviewing the API contract.
@router.get("/metrics")
def get_metrics(_agent=Depends(require_agent)):
    return _metrics.get_metrics_data()


@router.get("/metrics/ai")
def get_ai_metrics(
    db: Session = Depends(get_db),
    _agent=Depends(require_agent),
):
    """Per-model cost, latency and failure breakdown from durable call traces.

    Backed by the `ai_call_traces` table rather than the in-process counters, so
    it survives a restart and can be split by model and operation.
    """
    return tracing.summarize(db)


@router.get("/metrics/ai/recent")
def get_recent_traces(
    limit: int = 20,
    db: Session = Depends(get_db),
    _agent=Depends(require_agent),
):
    """The most recent LLM calls, newest first, for debugging a live issue."""
    from app.models import AICallTrace

    limit = max(1, min(limit, 100))
    rows = (
        db.query(AICallTrace)
        .order_by(AICallTrace.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": row.id,
                "request_id": row.request_id,
                "ticket_id": row.ticket_id,
                "operation": row.operation,
                "provider": row.provider,
                "model": row.model,
                "prompt_tokens": row.prompt_tokens,
                "completion_tokens": row.completion_tokens,
                "cost_usd": row.cost_usd,
                "usage_estimated": row.usage_estimated,
                "latency_ms": row.latency_ms,
                "outcome": row.outcome,
                "error_kind": row.error_kind,
                "confidence": row.confidence,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


class MetricsSnapshot:
    """Pydantic-free plain object — kept so existing response shape is unchanged."""

    def __init__(self, ai_calls: int, ai_errors: int, p50_latency_ms: int, avg_confidence: float | None):
        self.ai_calls = ai_calls
        self.ai_errors = ai_errors
        self.p50_latency_ms = p50_latency_ms
        self.avg_confidence = avg_confidence
