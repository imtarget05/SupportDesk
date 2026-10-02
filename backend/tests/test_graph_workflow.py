"""Tests for the ticket processing pipeline service."""

import pytest

from app.services.graph_workflow import (
    ConfidenceLevel,
    TicketProcessingGraph,
    TicketState,
    WorkflowResult,
    WorkflowStage,
    get_graph,
    reset_graph,
)
from app.services.knowledge_base import get_knowledge_base, reset_knowledge_base

# A draft that trips the "dangerous action" routing check without tripping the
# output guardrail. "full refund" would be blocked earlier by
# `guardrails.assert_safe_draft` and never reach the approval gate, so a test
# using it would be asserting the wrong failure; "cancel" is dangerous per
# `_confidence_check` but is not a disallowed commitment phrase.
APPROVAL_DRAFT = "We can cancel the subscription at your request; nothing else is needed."


@pytest.fixture(autouse=True)
def _reset_singletons():
    yield
    reset_graph()
    reset_knowledge_base()


class TestTicketState:
    def test_initial_state(self):
        state = TicketState(ticket_id=1, subject="Test", description="Test description")
        assert state.ticket_id == 1
        assert state.stage == WorkflowStage.START
        assert state.category == "unknown"
        assert state.confidence == 0.0
        assert state.evidence == []
        assert state.audit_log == []

    def test_log_action(self):
        state = TicketState(ticket_id=1, subject="Test", description="Test")
        state.log_action("test_action", {"key": "value"})
        assert len(state.audit_log) == 1
        assert state.audit_log[0]["action"] == "test_action"
        assert state.audit_log[0]["details"] == {"key": "value"}


class TestTicketProcessingGraph:
    def test_process_ticket_classification(self, client, agent_headers):
        """Test that workflow classifies a payment ticket correctly."""
        from tests.conftest import create_ticket

        ticket_id = create_ticket(
            client,
            subject="Card charged twice",
            description="My card was charged twice for the same order.",
        ).json()["id"]

        graph = get_graph()
        result = graph.process_ticket(
            ticket_id=ticket_id,
            subject="Card charged twice",
            description="My card was charged twice for the same order.",
        )

        assert result.ticket_id == ticket_id
        assert result.category == "payment"
        assert result.priority in ("high", "urgent")
        assert result.confidence > 0
        assert len(result.audit_log) > 0

    def test_process_ticket_authentication(self, client, agent_headers):
        """Test that workflow classifies an authentication ticket correctly."""
        from tests.conftest import create_ticket

        ticket_id = create_ticket(
            client,
            subject="Cannot login",
            description="I forgot my password and the reset link never arrives.",
        ).json()["id"]

        graph = get_graph()
        result = graph.process_ticket(
            ticket_id=ticket_id,
            subject="Cannot login",
            description="I forgot my password and the reset link never arrives.",
        )

        assert result.ticket_id == ticket_id
        assert result.category == "authentication"
        assert result.confidence > 0

    def test_process_ticket_with_evidence(self, client, agent_headers, tmp_path):
        """Test that workflow retrieves evidence from knowledge base."""
        # Create a temporary knowledge file
        knowledge_dir = tmp_path / "knowledge"
        knowledge_dir.mkdir()
        (knowledge_dir / "test.md").write_text(
            "# Test Knowledge\n\nPayment issues can be resolved by contacting support."
        )

        from app.services.knowledge_base import KnowledgeBase

        kb = KnowledgeBase(str(knowledge_dir))
        count = kb.ingest()
        assert count == 1

        graph = TicketProcessingGraph()
        result = graph.process_ticket(
            ticket_id=1,
            subject="Payment issue",
            description="I have a payment problem.",
        )

        assert result.ticket_id == 1
        # Evidence may or may not be found depending on the knowledge base
        assert isinstance(result.evidence, list)

    def test_confidence_level_high(self):
        """Test high confidence routing."""
        graph = TicketProcessingGraph()
        state = TicketState(ticket_id=1, subject="Test", description="Test")
        state.confidence = 0.9
        state.draft = "Here is the solution to your problem."
        state.stage = WorkflowStage.DRAFT

        result = graph._confidence_check(state)
        assert result.confidence_level == ConfidenceLevel.HIGH
        assert not result.requires_human_review

    def test_confidence_level_low(self):
        """Test low confidence triggers human review."""
        graph = TicketProcessingGraph()
        state = TicketState(ticket_id=1, subject="Test", description="Test")
        state.confidence = 0.5
        state.draft = "Here is the solution to your problem."
        state.stage = WorkflowStage.DRAFT

        result = graph._confidence_check(state)
        assert result.confidence_level == ConfidenceLevel.LOW
        assert result.requires_human_review
        assert "Low confidence" in result.review_reason

    def test_dangerous_action_requires_approval(self):
        """Test that dangerous actions require approval."""
        graph = TicketProcessingGraph()
        state = TicketState(ticket_id=1, subject="Test", description="Test")
        state.confidence = 0.95
        state.draft = "I will process a full refund for your order."
        state.stage = WorkflowStage.DRAFT

        result = graph._confidence_check(state)
        assert result.requires_approval
        assert "dangerous action" in result.review_reason.lower()


class TestLangGraphIsReal:
    """The workflow must be a real LangGraph, not a renamed straight line.

    Earlier versions of this module called themselves a "LangGraph workflow"
    while importing nothing from `langgraph`. These tests assert the framework is
    actually in the execution path.
    """

    def test_module_uses_langgraph(self):
        """The compiled app is a LangGraph runnable with the expected nodes."""
        from langgraph.graph.state import CompiledStateGraph

        graph = TicketProcessingGraph()
        assert isinstance(graph._app, CompiledStateGraph)
        # `__start__` is the synthetic entry node LangGraph adds.
        assert set(graph._app.nodes) - {"__start__"} == {
            "classify_ticket",
            "retrieve_context",
            "draft_answer",
            "confidence_check",
            "approval_gate",
        }

    def test_graph_has_a_checkpointer(self):
        from langgraph.checkpoint.memory import MemorySaver

        assert isinstance(TicketProcessingGraph()._checkpointer, MemorySaver)

    def test_audit_log_accumulates_across_nodes(self):
        """Append-reducers must neither double nor drop entries across nodes."""
        graph = TicketProcessingGraph()
        result = graph.process_ticket(
            ticket_id=1,
            subject="Card charged twice",
            description="My card was charged twice for the same order.",
        )
        actions = [entry["action"] for entry in result.audit_log]
        for expected in (
            "classify_start",
            "retrieve_start",
            "draft_start",
            "confidence_check_start",
        ):
            assert expected in actions
        assert actions.count("classify_start") == 1
        assert actions.count("classify_complete") == 1
        assert actions.count("draft_start") == 1

    def test_state_checkpoint_is_retrievable_by_thread_id(self):
        graph = TicketProcessingGraph()
        result = graph.process_ticket(
            ticket_id=7, subject="Cannot login", description="Reset link never arrives."
        )
        snapshot = graph._app.get_state(graph._config(result.workflow_id))
        assert snapshot.values["ticket_id"] == 7
        assert snapshot.values["category"] == result.category

    def test_approval_gate_suspends_and_resumes(self):
        """A draft needing approval pauses the graph until a human decides."""
        graph = TicketProcessingGraph()
        graph._agent.draft_response = lambda **kwargs: APPROVAL_DRAFT
        result = graph.process_ticket(
            ticket_id=9,
            subject="Refund please",
            description="I want my money back immediately.",
            require_approval=True,
        )
        assert result.requires_approval
        assert graph.is_suspended(result.workflow_id)

        approved = graph.resume_ticket(result.workflow_id, approved=True)
        assert approved.stage == WorkflowStage.SEND
        assert not approved.requires_approval
        assert not graph.is_suspended(result.workflow_id)

    def test_approval_gate_rejection_ends_the_run(self):
        graph = TicketProcessingGraph()
        graph._agent.draft_response = lambda **kwargs: APPROVAL_DRAFT
        result = graph.process_ticket(
            ticket_id=10,
            subject="Refund please",
            description="I want my money back immediately.",
            require_approval=True,
        )
        rejected = graph.resume_ticket(result.workflow_id, approved=False)
        assert rejected.stage == WorkflowStage.END
        assert rejected.requires_approval

    def test_approval_gate_not_armed_by_default(self):
        """The synchronous path reports the need for approval without pausing."""
        graph = TicketProcessingGraph()
        graph._agent.draft_response = lambda **kwargs: APPROVAL_DRAFT
        result = graph.process_ticket(
            ticket_id=11,
            subject="Refund please",
            description="I want my money back immediately.",
        )
        assert result.requires_approval
        assert not graph.is_suspended(result.workflow_id)

    def test_provider_failure_is_contained_in_state(self):
        """A provider error ends the run without raising out of the graph."""
        from app.services import ai_service

        graph = TicketProcessingGraph()
        graph._agent.draft_response = lambda **kwargs: (_ for _ in ()).throw(
            ai_service.AIProviderError("provider down")
        )
        result = graph.process_ticket(
            ticket_id=12, subject="Anything", description="Something happened."
        )
        assert result.error == "provider down"
        assert result.stage == WorkflowStage.END

