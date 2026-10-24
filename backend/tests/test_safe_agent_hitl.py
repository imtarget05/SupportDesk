"""Tests for Safe AI Agent with Tools and Human-In-The-Loop (HITL) approval gates.

Verifies:
1. Read-only tools (search_knowledge_base, get_customer_history, lookup_order)
   return factual evidence and never mutate database records.
2. Sensitive operations (refund, escalation, external reply) cannot be executed
   autonomously by the model — they generate an ActionProposal in 'pending_approval'.
3. Human rejection halts the action and records reviewer rationale in the audit trail.
4. Human approval executes the action, updates state, and records audit trail.
5. Authorization boundary: customers cannot approve proposals (403 Forbidden).
6. State monotonicity: already decided proposals cannot be re-approved/re-rejected (409 Conflict).
7. End-to-end agent run: tool calling proposes sensitive action -> pending proposal -> human approval.
"""

from __future__ import annotations

import json
import pytest

from app.models import ActionProposal, AuditEvent, Ticket, User
from app.services import hitl_service
from app.services.tool_agent import KeywordPlanner, ToolCallingAgent
from app.tools.registry import (
    CustomerHistoryTool,
    KnowledgeSearchTool,
    OrderLookupTool,
    ProposeEscalationTool,
    ProposeRefundTool,
    build_hitl_registry,
    build_registry,
)
from tests.conftest import create_ticket


class TestReadOnlyTools:
    def test_customer_history_tool(self, db_session):
        user = User(name="Alice Smith", email="alice@example.com", role="customer")
        db_session.add(user)
        db_session.commit()

        # Add 3 tickets: 2 open, 1 closed
        t1 = Ticket(
            customer_id=user.id,
            subject="First ticket",
            description="Details",
            status="open",
        )
        t2 = Ticket(
            customer_id=user.id,
            subject="Second ticket",
            description="Details",
            status="pending",
        )
        t3 = Ticket(
            customer_id=user.id,
            subject="Third ticket",
            description="Details",
            status="closed",
        )
        db_session.add_all([t1, t2, t3])
        db_session.commit()

        tool = CustomerHistoryTool(db_session)
        result = tool.run({"customer_id": user.id})

        assert result["customer_id"] == user.id
        assert result["total_tickets"] == 3
        assert result["open_tickets"] == 2
        assert result["resolved_tickets"] == 1
        assert result["sla_tier"] == "standard"
        assert len(result["recent_tickets"]) == 3

    def test_knowledge_search_tool_read_only(self):
        tool = KnowledgeSearchTool()
        result = tool.run({"query": "refund policy", "limit": 3})
        assert "evidence" in result
        assert isinstance(result["evidence"], list)

    def test_order_lookup_tool_read_only(self, db_session):
        user = User(name="Bob", email="bob@example.com", role="customer")
        db_session.add(user)
        db_session.commit()
        ticket = Ticket(
            customer_id=user.id,
            subject="Order status",
            description="Where is order",
            status="open",
        )
        db_session.add(ticket)
        db_session.commit()

        tool = OrderLookupTool(db_session)
        result = tool.run({"ticket_id": ticket.id})
        assert result["reference"] == f"TKT-{ticket.id}"
        assert result["state"] == "open"


class TestProposalCreationAndSafety:
    def test_propose_refund_creates_pending_proposal(self, db_session):
        user = User(name="Charlie", email="charlie@example.com", role="customer")
        db_session.add(user)
        db_session.commit()
        ticket = Ticket(
            customer_id=user.id,
            subject="Double charge",
            description="Card charged twice",
            status="open",
        )
        db_session.add(ticket)
        db_session.commit()

        tool = ProposeRefundTool(db_session)
        res = tool.run(
            {
                "ticket_id": ticket.id,
                "amount": 49.99,
                "reason": "Customer was double charged on renewal",
            }
        )

        assert res["action_type"] == "refund"
        assert res["status"] == "pending_approval"
        assert res["amount"] == 49.99
        assert "No money was transferred" in res["message"]

        # Verify proposal exists in DB
        proposal = db_session.get(ActionProposal, res["proposal_id"])
        assert proposal is not None
        assert proposal.status == "pending_approval"
        assert proposal.ticket_id == ticket.id
        payload = json.loads(proposal.payload)
        assert payload["amount"] == 49.99

        # Verify audit event for proposal creation
        events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.ticket_id == ticket.id,
                AuditEvent.action == "proposal_created",
            )
            .all()
        )
        assert len(events) == 1

    def test_propose_escalation_does_not_mutate_ticket_state(self, db_session):
        user = User(name="Dave", email="dave@example.com", role="customer")
        db_session.add(user)
        db_session.commit()
        ticket = Ticket(
            customer_id=user.id,
            subject="Urgent issue",
            description="Critical outage",
            status="open",
            priority="normal",
            escalation_level=0,
        )
        db_session.add(ticket)
        db_session.commit()

        tool = ProposeEscalationTool(db_session)
        res = tool.run(
            {
                "ticket_id": ticket.id,
                "escalation_level": 2,
                "reason": "Severity 1 outage requires Tier 2 attention",
            }
        )

        assert res["action_type"] == "escalate"
        assert res["status"] == "pending_approval"

        # Ticket must remain UNCHANGED before approval
        db_session.refresh(ticket)
        assert ticket.escalation_level == 0
        assert ticket.priority == "normal"


class TestHITLApprovalAndRejectionFlow:
    def test_approval_executes_escalation_and_records_audit(self, db_session):
        user = User(name="Eve", email="eve@example.com", role="customer")
        db_session.add(user)
        db_session.commit()
        ticket = Ticket(
            customer_id=user.id,
            subject="Enterprise blocker",
            description="Server down",
            status="open",
            priority="normal",
            escalation_level=0,
        )
        db_session.add(ticket)
        db_session.commit()

        proposal = hitl_service.create_proposal(
            db_session,
            ticket_id=ticket.id,
            action_type="escalate",
            payload={"escalation_level": 2, "reason": "SLA risk"},
            reasoning="Customer requested escalation",
            created_by="ai_agent",
        )
        assert proposal.status == "pending_approval"

        # Agent approves
        approved = hitl_service.approve_proposal(
            db_session,
            proposal.id,
            decided_by="lead_agent@example.com",
            reason="Confirmed critical severity",
        )

        assert approved.status == "approved"
        assert approved.decided_by == "lead_agent@example.com"
        assert approved.decision_reason == "Confirmed critical severity"
        assert approved.decided_at is not None

        # Verify ticket was actually updated
        db_session.refresh(ticket)
        assert ticket.escalation_level == 2
        assert ticket.priority == "urgent"

        # Verify audit trail
        audit_events = (
            db_session.query(AuditEvent)
            .filter(AuditEvent.ticket_id == ticket.id)
            .order_by(AuditEvent.id.asc())
            .all()
        )
        actions = [e.action for e in audit_events]
        assert "proposal_created" in actions
        assert "ticket_escalated" in actions
        assert "proposal_approved" in actions

    def test_rejection_halts_action_and_leaves_state_intact(self, db_session):
        user = User(name="Frank", email="frank@example.com", role="customer")
        db_session.add(user)
        db_session.commit()
        ticket = Ticket(
            customer_id=user.id,
            subject="Refund request",
            description="Item bought 90 days ago",
            status="open",
        )
        db_session.add(ticket)
        db_session.commit()

        proposal = hitl_service.create_proposal(
            db_session,
            ticket_id=ticket.id,
            action_type="refund",
            payload={"amount": 200.0, "reason": "Buyer remorse"},
            reasoning="Customer asking for money back",
            created_by="ai_agent",
        )

        # Agent rejects because 90 days exceeds policy
        rejected = hitl_service.reject_proposal(
            db_session,
            proposal.id,
            decided_by="senior_agent@example.com",
            reason="Exceeds 30-day return policy window",
        )

        assert rejected.status == "rejected"
        assert rejected.decided_by == "senior_agent@example.com"
        assert rejected.decision_reason == "Exceeds 30-day return policy window"

        # Verify execution result shows halted
        exec_res = json.loads(rejected.execution_result)
        assert exec_res["executed"] is False
        assert exec_res["halted"] is True

        # Verify audit trail
        audit_events = (
            db_session.query(AuditEvent)
            .filter(
                AuditEvent.ticket_id == ticket.id,
                AuditEvent.action == "proposal_rejected",
            )
            .all()
        )
        assert len(audit_events) == 1
        assert "30-day" in audit_events[0].details


class TestAPIEndpointsAndAuthorization:
    def test_api_proposals_list_and_approval(self, client, agent_headers):
        # Create ticket
        t_res = create_ticket(
            client,
            subject="Charge duplicate",
            description="Charged twice for subscription",
        )
        ticket_id = t_res.json()["id"]

        # Directly run propose tool through registry
        from app.database import SessionLocal

        with SessionLocal() as db:
            hitl_service.create_proposal(
                db,
                ticket_id=ticket_id,
                action_type="refund",
                payload={"amount": 35.0, "reason": "Double billing"},
                reasoning="Identical transaction IDs",
            )

        # Agent lists proposals
        list_res = client.get(
            f"/api/tickets/{ticket_id}/proposals", headers=agent_headers
        )
        assert list_res.status_code == 200
        proposals = list_res.json()
        assert len(proposals) == 1
        proposal_id = proposals[0]["id"]
        assert proposals[0]["status"] == "pending_approval"
        assert proposals[0]["payload"]["amount"] == 35.0

        # Agent approves proposal
        approve_res = client.post(
            f"/api/tickets/{ticket_id}/proposals/{proposal_id}/approve",
            json={"approved": True, "reason": "Verified bank statement"},
            headers=agent_headers,
        )
        assert approve_res.status_code == 200
        approved_data = approve_res.json()
        assert approved_data["status"] == "approved"
        assert approved_data["decision_reason"] == "Verified bank statement"

        # Cannot re-approve (conflict 409)
        re_approve_res = client.post(
            f"/api/tickets/{ticket_id}/proposals/{proposal_id}/approve",
            json={"approved": True, "reason": "Second try"},
            headers=agent_headers,
        )
        assert re_approve_res.status_code == 409

    def test_customer_cannot_approve_or_reject_proposal(
        self, client, customer_headers
    ):
        t_res = create_ticket(
            client, subject="Test ticket", description="Test description"
        )
        ticket_id = t_res.json()["id"]

        from app.database import SessionLocal

        with SessionLocal() as db:
            prop = hitl_service.create_proposal(
                db,
                ticket_id=ticket_id,
                action_type="refund",
                payload={"amount": 25.0},
                reasoning="Refund attempt",
            )
            prop_id = prop.id

        # Customer attempts to approve -> 403 Forbidden
        approve_res = client.post(
            f"/api/tickets/{ticket_id}/proposals/{prop_id}/approve",
            json={"approved": True},
            headers=customer_headers,
        )
        assert approve_res.status_code == 403

        # Customer attempts to reject -> 403 Forbidden
        reject_res = client.post(
            f"/api/tickets/{ticket_id}/proposals/{prop_id}/reject",
            json={"approved": False},
            headers=customer_headers,
        )
        assert reject_res.status_code == 403


class TestEndToEndAgentHITL:
    def test_agent_run_proposes_refund_and_waits_for_approval(self, db_session):
        user = User(name="Grace", email="grace@example.com", role="customer")
        db_session.add(user)
        db_session.commit()
        ticket = Ticket(
            customer_id=user.id,
            subject="Refund request for charged twice",
            description="I was charged twice on my card, please refund the second charge.",
            status="open",
        )
        db_session.add(ticket)
        db_session.commit()

        registry = build_hitl_registry(db_session)
        planner = KeywordPlanner(
            ticket_id=ticket.id, customer_id=user.id, enable_proposals=True
        )
        agent = ToolCallingAgent(registry=registry, planner=planner, max_steps=5)

        result = agent.run(
            subject=ticket.subject,
            description=ticket.description,
            thread="",
        )

        assert result.draft != ""
        # Check that propose_refund was called
        calls = [c["name"] for c in result.tool_calls]
        assert "propose_refund" in calls

        # Verify proposal exists in DB
        proposals = hitl_service.list_proposals(
            db_session, ticket_id=ticket.id, status="pending_approval"
        )
        assert len(proposals) == 1
        prop = proposals[0]
        assert prop.action_type == "refund"

        # Now simulate human agent review and approval
        approved = hitl_service.approve_proposal(
            db_session,
            prop.id,
            decided_by="supervisor@example.com",
            reason="Billing log confirms duplicate transaction ID #8892",
        )
        assert approved.status == "approved"
        assert approved.execution_result is not None
        exec_info = json.loads(approved.execution_result)
        assert exec_info["action"] == "refund"
        assert exec_info["executed"] is True
