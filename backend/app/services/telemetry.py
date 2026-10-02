"""OpenTelemetry instrumentation for LLM calls.

Emits spans following the OpenTelemetry GenAI semantic conventions
(``gen_ai.*``) so traces are readable by any OTLP-compatible backend (Langfuse,
Honeycomb, Grafana Tempo, a vendor collector) without changing code again.

Disabled by default via ``OTEL_ENABLED``. With tracing off, this module is a
no-op and adds nothing to the request path — the durable ``ai_call_traces``
table and the in-process counters still work, so observability is never a hard
dependency of the feature.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from app.config import settings
from app.services import pricing

logger = logging.getLogger(__name__)

# OTel GenAI semantic convention attribute names.
ATTR_SYSTEM = "gen_ai.system"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_USAGE_INPUT = "gen_ai.usage.input_tokens"
ATTR_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
ATTR_PROMPT = "gen_ai.prompt"
ATTR_COMPLETION = "gen_ai.completion"
ATTR_OPERATION = "gen_ai.operation.name"

_provider_initialized = False


def _init_provider() -> None:
    """Configure a TracerProvider once, exporting over OTLP when possible.

    Falls back to the console exporter so spans are still visible in logs when
    no collector endpoint is configured.
    """
    global _provider_initialized
    if _provider_initialized or not settings.otel_enabled:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create({"service.name": settings.otel_service_name})
        )
        provider.add_span_processor(BatchSpanProcessor(_make_exporter()))
        trace.set_tracer_provider(provider)
        _provider_initialized = True
    except Exception:  # noqa: BLE001 — observability must never break the app
        logger.warning("OpenTelemetry init failed; continuing without tracing", exc_info=True)


def _make_exporter():
    """Build the span exporter.

    OTLP when a collector endpoint is configured, console otherwise. Split out
    so tests can substitute a no-op exporter instead of reaching for a port.
    """
    if settings.otel_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        return OTLPSpanExporter(endpoint=settings.otel_otlp_endpoint)
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    return ConsoleSpanExporter()


def reset() -> None:
    """Test hook: allow re-initialization after settings change."""
    global _provider_initialized
    _provider_initialized = False


@contextmanager
def llm_span(operation: str, model: str, provider: str) -> Iterator[dict[str, Any]]:
    """Span one LLM call; attributes can be set on the yielded dict.

    Yields a plain dict rather than a span object so callers do not have to know
    whether tracing is enabled.
    """
    if not settings.otel_enabled:
        yield {}
        return

    _init_provider()
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("supportdesk.ai")
        with tracer.start_as_current_span(f"{operation} {model}") as span:
            span.set_attribute(ATTR_SYSTEM, provider)
            span.set_attribute(ATTR_REQUEST_MODEL, model)
            span.set_attribute(ATTR_OPERATION, operation)
            yield {"span": span}
    except Exception:  # noqa: BLE001 — never let tracing break an AI call
        logger.warning("Failed to create LLM span", exc_info=True)
        yield {}


def set_usage(attributes: dict[str, Any], model: str, usage: Any) -> None:
    """Attach token counts and the derived cost to an open span."""
    span = attributes.get("span")
    if span is None:
        return
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    span.set_attribute(ATTR_USAGE_INPUT, prompt_tokens)
    span.set_attribute(ATTR_USAGE_OUTPUT, completion_tokens)
    # Custom attribute: not part of the convention, but the number an engineer
    # actually needs when deciding whether to keep a model on.
    span.set_attribute("supportdesk.cost_usd", pricing.cost_usd(model, prompt_tokens, completion_tokens))
