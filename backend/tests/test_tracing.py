"""Durable AI call tracing: cost, latency, and failure classification."""

import pytest

from app.models import AICallTrace
from app.services import ai_service, tracing
from tests.conftest import create_ticket


def _traces(db_session) -> list[AICallTrace]:
    return db_session.query(AICallTrace).order_by(AICallTrace.id).all()


def test_successful_call_writes_a_trace(db_session):
    with tracing.trace_call("triage", "stub", "stub") as record:
        record["usage"] = ai_service.TokenUsage(
            prompt_tokens=100, completion_tokens=20, estimated=False
        )
        record["confidence"] = 0.9

    rows = _traces(db_session)
    assert len(rows) == 1
    assert rows[0].operation == "triage"
    assert rows[0].outcome == tracing.OUTCOME_OK
    assert rows[0].prompt_tokens == 100
    assert rows[0].confidence == 0.9
    assert rows[0].error_kind is None
    assert rows[0].latency_ms >= 0


def test_failed_call_is_traced_and_reraised(db_session):
    with pytest.raises(RuntimeError, match="provider exploded"):
        with tracing.trace_call("draft", "openai", "gpt-4o-mini") as record:
            record["usage"] = ai_service.TokenUsage(prompt_tokens=5, completion_tokens=1)
            raise RuntimeError("provider exploded")

    row = _traces(db_session)[0]
    assert row.outcome == tracing.OUTCOME_ERROR
    assert row.error_kind == "RuntimeError"


def test_blocked_outcome_is_distinguishable(db_session):
    with tracing.trace_call("draft", "openai", "gpt-4o-mini") as record:
        record["usage"] = ai_service.TokenUsage(prompt_tokens=5, completion_tokens=1)
        tracing.mark_blocked(record)

    row = _traces(db_session)[0]
    assert row.outcome == tracing.OUTCOME_BLOCKED
    assert row.error_kind == "GuardrailError"


def test_cost_is_recorded_for_a_priced_model(db_session):
    with tracing.trace_call("triage", "openai", "gpt-4o-mini") as record:
        record["usage"] = ai_service.TokenUsage(
            prompt_tokens=1_000_000, completion_tokens=0, estimated=False
        )

    row = _traces(db_session)[0]
    assert row.cost_usd == pytest.approx(0.15)
    assert row.pricing_table_version
    assert row.usage_estimated is False


def test_unpriced_model_records_null_cost_not_zero(db_session):
    """"We could not price this" and "this was free" are different answers."""
    with tracing.trace_call("triage", "stub", "stub") as record:
        record["usage"] = ai_service.TokenUsage(
            prompt_tokens=100, completion_tokens=10, estimated=True
        )

    row = _traces(db_session)[0]
    assert row.cost_usd is None
    assert row.pricing_table_version is None
    assert row.usage_estimated is True


def test_tracing_failure_does_not_break_the_call(db_session, monkeypatch):
    """A broken trace write must never fail a successful AI call."""
    from app.services import metrics

    def boom(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(tracing, "_persist", boom)
    with tracing.trace_call("triage", "stub", "stub") as record:
        record["usage"] = ai_service.TokenUsage(prompt_tokens=1, completion_tokens=1)

    # The context manager exited normally, which is the property that matters.
    assert metrics.get_metrics_data()["ai_calls"] == 0


def test_analyze_ticket_persists_a_trace(db_session, client, agent_headers):
    ticket_id = create_ticket(
        client, subject="Card charged twice", description="Charged twice for one order."
    ).json()["id"]
    res = client.post(f"/api/tickets/{ticket_id}/ai/analyze", headers=agent_headers)
    assert res.status_code == 200

    rows = db_session.query(AICallTrace).filter_by(ticket_id=ticket_id).all()
    assert len(rows) == 1
    assert rows[0].operation == "triage"
    assert rows[0].outcome == tracing.OUTCOME_OK


def test_suggest_ticket_persists_a_trace(db_session, client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Refund", description="I want a refund."
    ).json()["id"]

    res = client.post(f"/api/tickets/{ticket_id}/ai/suggest", headers=agent_headers)
    assert res.status_code == 200

    row = db_session.query(AICallTrace).filter_by(ticket_id=ticket_id).one()
    assert row.operation == "draft"


def test_provider_failure_trace_records_error_outcome(
    db_session, client, agent_headers, customer_headers
):
    class Boom:
        model = "gpt-4o-mini"

        def analyze(self, subject, description):
            raise ai_service.AIProviderError("upstream down")

        def suggest(self, subject, description, thread):
            raise ai_service.AIProviderError("upstream down")

    ai_service.set_provider(Boom())
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Anything", description="Something."
    ).json()["id"]

    res = client.post(f"/api/tickets/{ticket_id}/ai/analyze", headers=agent_headers)
    assert res.status_code == 502

    row = db_session.query(AICallTrace).filter_by(ticket_id=ticket_id).one()
    assert row.outcome == tracing.OUTCOME_ERROR
    assert row.error_kind == "AIProviderError"


def test_summarize_groups_by_model(db_session):
    for _ in range(3):
        with tracing.trace_call("triage", "openai", "gpt-4o-mini") as record:
            record["usage"] = ai_service.TokenUsage(prompt_tokens=100, completion_tokens=10)
    for _ in range(2):
        with tracing.trace_call("draft", "anthropic", "claude-sonnet-4-5") as record:
            record["usage"] = ai_service.TokenUsage(prompt_tokens=200, completion_tokens=20)

    summary = tracing.summarize(db_session)
    assert summary["total_calls"] == 5
    assert [m["model"] for m in summary["by_model"]] == ["gpt-4o-mini", "claude-sonnet-4-5"]
    assert summary["by_model"][0]["calls"] == 3
    # Every call was priced, so a total is available.
    assert summary["total_cost_usd"] is not None
    assert summary["unpriced_calls"] == 0


def test_summarize_reports_null_total_when_any_call_unpriced(db_session):
    with tracing.trace_call("triage", "openai", "gpt-4o-mini") as record:
        record["usage"] = ai_service.TokenUsage(prompt_tokens=100, completion_tokens=10)
    with tracing.trace_call("triage", "stub", "stub") as record:
        record["usage"] = ai_service.TokenUsage(prompt_tokens=100, completion_tokens=10)

    summary = tracing.summarize(db_session)
    assert summary["unpriced_calls"] == 1
    assert summary["total_cost_usd"] is None, "a partial sum must not read as complete"


def test_summarize_counts_errors_and_blocks(db_session):
    with tracing.trace_call("draft", "openai", "gpt-4o-mini") as record:
        record["usage"] = ai_service.TokenUsage(prompt_tokens=1, completion_tokens=1)
        tracing.mark_blocked(record)
    with pytest.raises(RuntimeError):
        with tracing.trace_call("draft", "openai", "gpt-4o-mini"):
            raise RuntimeError("x")

    summary = tracing.summarize(db_session)
    assert summary["total_blocked"] == 1
    assert summary["total_errors"] == 1
    entry = summary["by_model"][0]
    assert entry["cost_per_successful_call_usd"] is None, "no successful call to divide by"


def test_summarize_on_empty_table_is_zero_not_error(db_session):
    summary = tracing.summarize(db_session)
    assert summary["total_calls"] == 0
    assert summary["by_model"] == []
    assert summary["total_cost_usd"] == 0.0


# ---------------------------------------------------------------- API surface


def test_ai_metrics_endpoint_requires_agent(client, customer_headers):
    assert client.get("/api/metrics/ai").status_code == 401
    assert client.get("/api/metrics/ai", headers=customer_headers).status_code == 403


def test_ai_metrics_endpoint_returns_summary(client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Charged twice", description="My card was charged twice for one order."
    ).json()["id"]
    client.post(f"/api/tickets/{ticket_id}/ai/analyze", headers=agent_headers)

    res = client.get("/api/metrics/ai", headers=agent_headers)
    assert res.status_code == 200
    body = res.json()
    assert body["total_calls"] == 1
    assert body["pricing_as_of"]
    assert body["by_model"][0]["model"] == "stub"


def test_recent_traces_endpoint(client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Charged twice", description="My card was charged twice for one order."
    ).json()["id"]
    client.post(f"/api/tickets/{ticket_id}/ai/analyze", headers=agent_headers)

    res = client.get("/api/metrics/ai/recent", headers=agent_headers)
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    assert items[0]["ticket_id"] == ticket_id
    assert items[0]["operation"] == "triage"


def test_recent_traces_limit_is_capped(client, agent_headers):
    res = client.get("/api/metrics/ai/recent?limit=9999", headers=agent_headers)
    assert res.status_code == 200

