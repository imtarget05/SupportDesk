"""OpenTelemetry instrumentation: inert when disabled, spans when enabled."""

import dataclasses

import pytest
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from app.services import ai_service, telemetry


class _DiscardingExporter(SpanExporter):
    """Accepts spans and drops them, so nothing is flushed at interpreter exit."""

    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


@pytest.fixture()
def no_exporter(monkeypatch):
    """Route spans to a discarding exporter.

    The real exporters either reach for the network or hold a reference to the
    stdout stream pytest has already closed, so both produce noisy background
    failures. The test suite must stay offline and silent.
    """
    monkeypatch.setattr(telemetry, "_make_exporter", _DiscardingExporter)


@pytest.fixture(autouse=True)
def _reset_telemetry():
    yield
    telemetry.reset()


def _enable(monkeypatch, **overrides):
    # Patch the settings object this module actually reads, not the one in
    # app.config: `from app.config import settings` binds a local name.
    monkeypatch.setattr(
        telemetry, "settings",
        dataclasses.replace(telemetry.settings, otel_enabled=True, **overrides),
    )
    telemetry.reset()


def test_disabled_by_default():
    assert telemetry.settings.otel_enabled is False


def test_llm_span_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(
        telemetry, "settings",
        dataclasses.replace(telemetry.settings, otel_enabled=False),
    )
    with telemetry.llm_span("triage", "gpt-4o-mini", "openai") as attrs:
        assert attrs == {}


def test_llm_span_yields_a_span_when_enabled(monkeypatch, no_exporter):
    _enable(monkeypatch, otel_service_name="test-svc")

    with telemetry.llm_span("triage", "gpt-4o-mini", "openai") as attrs:
        # A real tracer is installed by this point.
        assert "span" in attrs
        span = attrs["span"]
        assert span.get_span_context().is_valid
        assert span.attributes[telemetry.ATTR_SYSTEM] == "openai"
        assert span.attributes[telemetry.ATTR_REQUEST_MODEL] == "gpt-4o-mini"
        assert span.attributes[telemetry.ATTR_OPERATION] == "triage"


def test_set_usage_attaches_tokens_and_cost(monkeypatch, no_exporter):
    _enable(monkeypatch, otel_service_name="test-svc")

    with telemetry.llm_span("triage", "gpt-4o-mini", "openai") as attrs:
        telemetry.set_usage(
            attrs,
            "gpt-4o-mini",
            ai_service.TokenUsage(prompt_tokens=1_000_000, completion_tokens=0),
        )
        span = attrs["span"]
        assert span.attributes[telemetry.ATTR_USAGE_INPUT] == 1_000_000
        assert span.attributes[telemetry.ATTR_USAGE_OUTPUT] == 0
        assert span.attributes["supportdesk.cost_usd"] == pytest.approx(0.15)


def test_set_usage_is_safe_without_a_span(monkeypatch):
    monkeypatch.setattr(
        telemetry, "settings",
        dataclasses.replace(telemetry.settings, otel_enabled=False),
    )
    # Must not raise even though there is nothing to set.
    telemetry.set_usage({}, "gpt-4o-mini", ai_service.TokenUsage(prompt_tokens=1))


def test_semconv_attribute_names_are_stable():
    """Renaming these silently would break every downstream dashboard."""
    assert telemetry.ATTR_SYSTEM == "gen_ai.system"
    assert telemetry.ATTR_REQUEST_MODEL == "gen_ai.request.model"
    assert telemetry.ATTR_USAGE_INPUT == "gen_ai.usage.input_tokens"
    assert telemetry.ATTR_USAGE_OUTPUT == "gen_ai.usage.output_tokens"
