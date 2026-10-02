"""Token usage and cost-attribution tests.

Covers the pricing table, the per-provider usage counters, and provider
selection for the Anthropic backend.
"""

import pytest

from app.services import ai_service, metrics, pricing


@pytest.fixture(autouse=True)
def _reset_state():
    metrics.reset()
    yield
    metrics.reset()
    ai_service.set_provider(None)


# --------------------------------------------------------------------- pricing


def test_cost_is_zero_for_no_tokens():
    assert pricing.cost_usd("gpt-4o-mini", 0, 0) == 0.0


def test_cost_matches_published_rate():
    # gpt-4o-mini: $0.15 / 1M input, $0.60 / 1M output.
    assert pricing.cost_usd("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert pricing.cost_usd("gpt-4o-mini", 0, 1_000_000) == pytest.approx(0.60)


def test_cost_combines_input_and_output():
    assert pricing.cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)


def test_unknown_model_returns_none_not_a_guess():
    """A missing price must surface as a gap, never as a fabricated number."""
    assert pricing.cost_usd("some-unlisted-model", 1000, 1000) is None
    assert pricing.is_priced("some-unlisted-model") is False


def test_dated_snapshot_prices_against_base_model():
    assert pricing.cost_usd("gpt-4o-2024-08-06", 1_000_000, 0) == pytest.approx(2.50)


def test_pricing_table_is_versioned():
    assert pricing.PRICING_TABLE_VERSION == pricing.PRICING_AS_OF


def test_estimate_tokens_scales_with_length():
    assert pricing.estimate_tokens("") == 0
    assert 0 < pricing.estimate_tokens("a" * 40) < pricing.estimate_tokens("a" * 400)


# ---------------------------------------------------------------- token usage


def test_stub_provider_reports_estimated_usage():
    provider = ai_service.StubProvider()
    provider.analyze("Card charged twice", "I was charged twice for one order.")
    assert provider.last_usage.estimated is True
    assert provider.last_usage.total_tokens > 0


def test_stub_provider_records_usage_on_suggest():
    provider = ai_service.StubProvider()
    provider.suggest("Refund", "I want a refund", "")
    assert provider.last_usage.estimated is True
    assert provider.last_usage.total_tokens > 0


def test_metrics_accumulate_tokens_from_stub():
    """The offline path must still produce a measurable token figure."""
    ai_service.analyze_ticket("Card charged twice", "Charged twice for one order.")
    data = metrics.get_metrics_data()
    assert data["ai_calls"] == 1
    assert data["total_tokens"] > 0
    # `stub` is not in the price table, so cost stays explicitly zero rather
    # than being invented.
    assert data["cost_usd"] == 0.0


def test_metrics_record_cost_for_a_priced_model():
    from app.enums import TicketCategory, TicketPriority

    class PricedProvider(ai_service._UsageRecorder):
        model = "gpt-4o-mini"

        def __init__(self):
            self.last_usage = ai_service.TokenUsage(
                prompt_tokens=1_000_000, completion_tokens=0, estimated=False
            )

        def analyze(self, subject, description):
            return ai_service.AnalysisResult(
                category=TicketCategory.PAYMENT,
                priority=TicketPriority.HIGH,
                summary="Charged twice",
                confidence=0.9,
            )

        def suggest(self, subject, description, thread):
            return "Sorry about that."

    ai_service.set_provider(PricedProvider())
    ai_service.analyze_ticket("Charged twice", "Charged twice for one order.")
    data = metrics.get_metrics_data()
    assert data["prompt_tokens"] == 1_000_000
    assert data["cost_usd"] == pytest.approx(0.15)


def test_percentile_is_reported():
    for latency in range(1, 101):
        metrics.record_call(latency_ms=latency, confidence=None)
    data = metrics.get_metrics_data()
    assert data["p50_latency_ms"] == 50
    assert data["p95_latency_ms"] >= 95


def test_reset_clears_token_and_cost_counters():
    metrics.record_call(
        latency_ms=10, confidence=0.5, prompt_tokens=100, completion_tokens=50, cost_usd=0.01
    )
    metrics.reset()
    data = metrics.get_metrics_data()
    assert data["total_tokens"] == 0
    assert data["cost_usd"] == 0.0


def test_active_model_falls_back_to_provider_name():
    class NoModel:
        pass

    assert ai_service._active_model(NoModel()) == ai_service.settings.ai_provider
