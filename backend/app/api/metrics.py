"""Metrics API endpoint — delegates to app.services.metrics for state.

This file is a thin FastAPI wrapper only. The actual counters live in
``app.services.metrics`` so ai_service can call record_call/record_error
without a circular import through the API layer.
"""

from fastapi import APIRouter, Depends

from app.deps import require_agent
from app.services import metrics as _metrics

router = APIRouter(tags=["metrics"])  # prefix added by include_router in main.py


# Operational counters only — agent-scoped. It used to be anonymous and
# hidden from the schema, which made an unauthenticated capability invisible
# to anyone reviewing the API contract.
@router.get("/metrics")
def get_metrics(_agent=Depends(require_agent)):
    return _metrics.get_metrics_data()


class MetricsSnapshot:
    """Pydantic-free plain object — kept so existing response shape is unchanged."""

    def __init__(self, ai_calls: int, ai_errors: int, p50_latency_ms: int, avg_confidence: float | None):
        self.ai_calls = ai_calls
        self.ai_errors = ai_errors
        self.p50_latency_ms = p50_latency_ms
        self.avg_confidence = avg_confidence
