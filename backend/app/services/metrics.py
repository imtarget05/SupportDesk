"""Metrics in-memory counters for AI layer.

Single source of truth for AI call counts, errors, latencies, and confidences.
Import from here (app.services.metrics) — not from app.api.metrics — to avoid
circularity when ai_service also updates counters.
"""

# In-memory state (process-level; reset for tests)
_counters = {
    "ai_calls": 0,
    "ai_errors": 0,
    "latencies": [],
    "confidences": [],
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cost_usd": 0.0,
}


def record_call(
    latency_ms: int,
    confidence: float | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float | None = None,
) -> None:
    """Record a successful AI provider call.

    Token and cost fields are optional so existing call sites keep working;
    a provider that reports no usage contributes zero tokens rather than a
    fabricated estimate.
    """
    _counters["ai_calls"] += 1
    _counters["latencies"].append(latency_ms)
    if confidence is not None:
        _counters["confidences"].append(confidence)
    if prompt_tokens:
        _counters["prompt_tokens"] += prompt_tokens
    if completion_tokens:
        _counters["completion_tokens"] += completion_tokens
    if cost_usd:
        _counters["cost_usd"] += cost_usd


def record_error() -> None:
    """Record an AI provider error."""
    _counters["ai_errors"] += 1


def _percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile over an already-sorted list."""
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, int(round((pct / 100) * len(sorted_values) + 0.5)) - 1)
    return int(sorted_values[max(0, index)])


def get_metrics_data() -> dict:
    """Return the current metrics snapshot (used by the API endpoint)."""
    _latency_sorted = sorted(_counters["latencies"])
    return {
        "ai_calls": _counters["ai_calls"],
        "ai_errors": _counters["ai_errors"],
        "p50_latency_ms": _percentile(_latency_sorted, 50),
        "p95_latency_ms": _percentile(_latency_sorted, 95),
        "prompt_tokens": _counters["prompt_tokens"],
        "completion_tokens": _counters["completion_tokens"],
        "total_tokens": _counters["prompt_tokens"] + _counters["completion_tokens"],
        "cost_usd": round(_counters["cost_usd"], 6),
        "avg_confidence": round(sum(_counters["confidences"]) / len(_counters["confidences"]), 2)
        if _counters["confidences"]
        else None,
    }


def reset() -> None:
    """Reset all counters (for tests)."""
    _counters["ai_calls"] = 0
    _counters["ai_errors"] = 0
    _counters["latencies"] = []
    _counters["confidences"] = []
    _counters["prompt_tokens"] = 0
    _counters["completion_tokens"] = 0
    _counters["cost_usd"] = 0.0


class MetricsSnapshot:
    """Pydantic model for the metrics endpoint response (kept for API backward compat)."""

    def __init__(self, ai_calls: int, ai_errors: int, p50_latency_ms: int, avg_confidence: float | None):
        self.ai_calls = ai_calls
        self.ai_errors = ai_errors
        self.p50_latency_ms = p50_latency_ms
        self.avg_confidence = avg_confidence