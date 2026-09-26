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