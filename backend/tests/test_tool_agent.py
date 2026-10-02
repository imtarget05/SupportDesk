"""Tool-calling agent loop: bounded steps, loop detection, and error containment."""

import pytest

from app.services import ai_service
from app.services.tool_agent import KeywordPlanner, ToolCall, ToolCallingAgent
from app.tools import ToolRegistry
from app.tools.base import BaseTool, TicketSearchArgs
from tests.conftest import create_ticket


class Recorder:
    """A planner that returns a scripted sequence of call batches."""

    def __init__(self, batches: list[list[ToolCall]]):
        self.batches = batches
        self.calls = 0

    def next_calls(self, subject, description, transcript):
        index = min(self.calls, len(self.batches) - 1)
        self.calls += 1
        return self.batches[index]


class BrokenPlanner:
    def next_calls(self, subject, description, transcript):
        raise RuntimeError("planner exploded")


def _echo_registry() -> ToolRegistry:
    class EchoTool(BaseTool):
        name = "echo"
        description = "Echo the query."
        args_model = TicketSearchArgs

        @property
        def handler(self):
            return lambda args: {"query": args.query}

    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


# ------------------------------------------------------------------ planner


def test_keyword_planner_finds_policy_questions():
    planner = KeywordPlanner()
    calls = planner.next_calls(
        "Return policy question", "what is the return window for this item", []
    )
    assert [c.name for c in calls] == ["search_knowledge_base"]


def test_keyword_planner_is_silent_on_the_second_turn():
    planner = KeywordPlanner()
    assert planner.next_calls("policy", "what is the policy", ["<tool_result>"]) == []


def test_keyword_planner_stays_silent_without_cues():
    planner = KeywordPlanner()
    assert planner.next_calls("Charged twice", "my card was charged twice", []) == []


def test_keyword_planner_truncates_a_long_query():
    planner = KeywordPlanner()
    call = planner.next_calls("policy", "x" * 1000, [])[0]
    assert len(call.arguments["query"]) <= 200


# --------------------------------------------------------------- happy path


def test_agent_returns_a_draft(db_session):
    result = ToolCallingAgent(_echo_registry()).run(
        "Cannot log in", "The reset link never arrives at all"
    )
    assert result.draft
    assert result.stopped_reason == "drafted"
# --------------------------------------------------------------- loop safety


def test_repeated_tool_call_is_stopped(db_session):
    """A planner stuck on one call must not loop forever."""
    planner = Recorder([[ToolCall("echo", {"query": "policy"})]])
    result = ToolCallingAgent(_echo_registry(), planner=planner, max_steps=10).run(
        "Policy question", "what does the policy say"
    )
    assert result.stopped_reason == "repeated_tool_call"


def test_step_ceiling_is_enforced(db_session):
    """Even with a changing call each turn, the run stops at the ceiling."""
    counter = {"n": 0}

    class VaryingPlanner:
        def next_calls(self, subject, description, transcript):
            counter["n"] += 1
            return [ToolCall("echo", {"query": f"query {counter['n']}"})]

    result = ToolCallingAgent(_echo_registry(), planner=VaryingPlanner(), max_steps=3).run(
        "Anything", "Something happened"
    )
    assert result.stopped_reason == "max_steps"
    assert counter["n"] <= 3


def test_max_steps_defaults_from_settings():
    from app.config import settings

    assert ToolCallingAgent(_echo_registry()).max_steps == settings.tool_max_steps


# ----------------------------------------------------------- error containment


def test_unknown_tool_is_reported_not_raised(db_session):
    planner = Recorder([[ToolCall("rm_minus_rf", {"query": "x"})], []])
    result = ToolCallingAgent(_echo_registry(), planner=planner).run(
        "Anything", "Something happened"
    )
    assert result.tool_calls[0]["ok"] is False
    assert "unknown tool" in result.tool_calls[0]["error"]
    assert result.draft, "a tool failure must not prevent a draft"


def test_invalid_tool_arguments_are_reported(db_session):
    planner = Recorder([[ToolCall("echo", {"query": "x"})], []])
    result = ToolCallingAgent(_echo_registry(), planner=planner).run(
        "Anything", "Something happened"
    )
    assert result.tool_calls[0]["ok"] is False
    assert "invalid arguments" in result.tool_calls[0]["error"]


def test_planner_error_is_contained(db_session):
    result = ToolCallingAgent(_echo_registry(), planner=BrokenPlanner()).run(
        "Anything", "Something happened"
    )
    assert result.stopped_reason == "planner_error"
    assert "planner exploded" in result.error
    assert result.draft == ""


def test_provider_error_is_contained(db_session):
    class Boom:
        model = "gpt-4o-mini"

        def analyze(self, subject, description):
            raise ai_service.AIProviderError("upstream down")

        def suggest(self, subject, description, thread):
            raise ai_service.AIProviderError("upstream down")

    ai_service.set_provider(Boom())
    result = ToolCallingAgent(_echo_registry()).run("Anything", "Something happened")
    assert result.stopped_reason == "provider_error"
    assert result.draft == ""


def test_unsafe_draft_is_not_returned(db_session):
    """A draft that trips the guardrail must surface as an error, not output."""

    class EvilProvider(ai_service.StubProvider):
        def suggest(self, subject, description, thread):
            return "I will process a full refund for you right away."

    ai_service.set_provider(EvilProvider())
    result = ToolCallingAgent(_echo_registry()).run(
        "Refund please", "I want my money back immediately"
    )
    assert result.draft == ""
    assert result.error


def test_tool_error_cannot_leak_into_the_draft(db_session):
    """A tool error string must not become the customer-facing answer.

    The model echoes the tool error into its draft, which is how an internal
    message like a database failure would otherwise leak into a reply.
    """

    class EchoesErrors(ai_service.StubProvider):
        def suggest(self, subject, description, thread):
            return f"Internal error: OperationalError no such table: tickets. {thread}"

    ai_service.set_provider(EchoesErrors())
    planner = Recorder([[ToolCall("echo", {"query": "policy"})], []])
    result = ToolCallingAgent(_echo_registry(), planner=planner).run(
        "Policy question", "what does the policy say"
    )
    assert result.draft == "", "an echoed tool error must be rejected, not returned"
    assert "internal detail" in result.error


# ---------------------------------------------------------------- API layer


def test_agent_endpoint_requires_agent(client, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Login issue", description="Locked out again."
    ).json()["id"]
    assert client.post(f"/api/tickets/{ticket_id}/ai/agent").status_code == 401
    assert (
        client.post(
            f"/api/tickets/{ticket_id}/ai/agent", headers=customer_headers
        ).status_code
        == 403
    )


def test_agent_endpoint_returns_draft_and_tool_calls(
    client, agent_headers, customer_headers
):
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Return policy question",
        description="How long is the return window for a physical product?",
    ).json()["id"]

    res = client.post(f"/api/tickets/{ticket_id}/ai/agent", headers=agent_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft"]
    assert body["stopped_reason"]
    assert body["steps"] >= 1
    # The policy cue should have driven a knowledge-base lookup.
    assert any(c["name"] == "search_knowledge_base" for c in body["tool_calls"])


def test_agent_endpoint_never_sends_a_message(
    client, agent_headers, customer_headers, db_session
):
    """The loop is suggestion-only: no message row may be created."""
    from app.models import Message

    before = db_session.query(Message).count()
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Return policy question",
        description="How long is the return window for a physical product?",
    ).json()["id"]
    client.post(f"/api/tickets/{ticket_id}/ai/agent", headers=agent_headers)
    assert db_session.query(Message).count() == before


def test_agent_endpoint_502_when_the_draft_is_blocked(
    client, agent_headers, customer_headers, db_session
):
    """A guardrail-blocked run withholds the draft rather than returning it."""
    from app.models import Message

    class EvilProvider(ai_service.StubProvider):
        def suggest(self, subject, description, thread):
            return "I will process a full refund for you right away."

    ai_service.set_provider(EvilProvider())
    before = db_session.query(Message).count()
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Refund please",
        description="I want my money back immediately please.",
    ).json()["id"]

    res = client.post(f"/api/tickets/{ticket_id}/ai/agent", headers=agent_headers)
    assert res.status_code == 502
    assert "guardrail" in res.json()["detail"]
    assert db_session.query(Message).count() == before


def test_agent_records_its_tool_calls(db_session):
    planner = Recorder([[ToolCall("echo", {"query": "policy"})], []])
    result = ToolCallingAgent(_echo_registry(), planner=planner).run(
        "Policy question", "what does the policy say"
    )
    assert [c["name"] for c in result.tool_calls] == ["echo"]
    assert result.tool_calls[0]["ok"] is True


def test_agent_keeps_a_step_history(db_session):
    result = ToolCallingAgent(_echo_registry()).run(
        "Cannot log in", "The reset link never arrives at all"
    )
    assert result.steps
    assert result.steps[-1].tool_calls == []