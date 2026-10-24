"""Human-In-The-Loop (HITL) Service for sensitive agent proposals.

Guarantees:
- Models suggest or propose actions; they NEVER execute sensitive operations
  (refunds, escalations, public messages) autonomously.
- Every proposed action is stored durably as an `ActionProposal` in `pending_approval`.
- Approval executes the operation and commits audit logs in the same transaction.
- Rejection halts the operation and records human rationale in the audit trail.
- State transitions are strictly monotonic: once decided, a proposal cannot be re-decided.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.message import Message
from app.models.proposal import ActionProposal
from app.models.ticket import Ticket
from app.models.user import User
from app.services import audit

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = {"refund", "escalate", "external_reply"}


def create_proposal(
    db: Session,
    *,
    ticket_id: int,
    action_type: str,
    payload: dict[str, Any],
    reasoning: str = "",
    confidence: float = 1.0,
    created_by: str = "ai_agent",
) -> ActionProposal:
    """Queue a sensitive action for human authorization."""
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise ValueError(f"Ticket {ticket_id} not found")

    proposal = ActionProposal(
        ticket_id=ticket_id,
        action_type=action_type,
        payload=json.dumps(payload, ensure_ascii=False),
        reasoning=reasoning,
        confidence=confidence,
        status="pending_approval",
        created_by=created_by,
    )
    db.add(proposal)
    db.flush()

    audit.log_event(
        db,
        action="proposal_created",
        ticket_id=ticket_id,
        actor=created_by,
        details={
            "proposal_id": proposal.id,
            "action_type": action_type,
            "reasoning": reasoning,
            "payload": payload,
        },
    )
    db.commit()
    db.refresh(proposal)
    return proposal


def get_proposal(db: Session, proposal_id: int) -> ActionProposal | None:
    return db.get(ActionProposal, proposal_id)


def list_proposals(
    db: Session,
    *,
    ticket_id: int | None = None,
    status: str | None = None,
) -> list[ActionProposal]:
    query = db.query(ActionProposal)
    if ticket_id is not None:
        query = query.filter(ActionProposal.ticket_id == ticket_id)
    if status is not None:
        query = query.filter(ActionProposal.status == status)
    return query.order_by(ActionProposal.id.desc()).all()


def approve_proposal(
    db: Session,
    proposal_id: int,
    *,
    decided_by: str,
    reason: str = "",
) -> ActionProposal:
    """Approve and execute a pending action proposal."""
    proposal = db.get(ActionProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")

    if proposal.status != "pending_approval":
        raise ValueError(
            f"Proposal {proposal_id} is already decided (status: {proposal.status})"
        )

    ticket = db.get(Ticket, proposal.ticket_id)
    if ticket is None:
        raise ValueError(f"Ticket {proposal.ticket_id} not found")

    payload = json.loads(proposal.payload)
    execution_result: dict[str, Any]

    if proposal.action_type == "refund":
        amount = float(payload.get("amount", 0.0))
        refund_reason = payload.get("reason", reason or "Customer requested refund")
        audit.log_event(
            db,
            action="refund_executed",
            ticket_id=ticket.id,
            actor=decided_by,
            details={
                "amount": amount,
                "reason": refund_reason,
                "proposal_id": proposal.id,
                "status": "processed",
            },
        )
        execution_result = {
            "executed": True,
            "action": "refund",
            "amount": amount,
            "status": "processed",
        }

    elif proposal.action_type == "escalate":
        level = int(payload.get("escalation_level", ticket.escalation_level + 1))
        ticket.escalation_level = level
        ticket.priority = "urgent"
        audit.log_event(
            db,
            action="ticket_escalated",
            ticket_id=ticket.id,
            actor=decided_by,
            details={
                "escalation_level": level,
                "priority": ticket.priority,
                "proposal_id": proposal.id,
                "reason": reason or payload.get("reason", ""),
            },
        )
        execution_result = {
            "executed": True,
            "action": "escalate",
            "escalation_level": level,
            "priority": ticket.priority,
        }

    elif proposal.action_type == "external_reply":
        content = payload.get("content", "")
        # Find agent user for sender
        agent_user = (
            db.query(User).filter(User.email == decided_by).first()
            or db.query(User).filter(User.role == "agent").first()
        )
        sender_id = agent_user.id if agent_user else ticket.customer_id
        message = Message(ticket_id=ticket.id, sender_id=sender_id, content=content)
        db.add(message)
        audit.log_event(
            db,
            action="message_sent",
            ticket_id=ticket.id,
            actor=decided_by,
            details={
                "message_length": len(content),
                "proposal_id": proposal.id,
            },
        )
        execution_result = {
            "executed": True,
            "action": "external_reply",
            "content": content,
        }

    else:
        audit.log_event(
            db,
            action="generic_action_executed",
            ticket_id=ticket.id,
            actor=decided_by,
            details={"action_type": proposal.action_type, "proposal_id": proposal.id},
        )
        execution_result = {"executed": True, "action": proposal.action_type}

    proposal.status = "approved"
    proposal.decided_by = decided_by
    proposal.decision_reason = reason
    proposal.decided_at = datetime.utcnow()
    proposal.execution_result = json.dumps(execution_result, ensure_ascii=False)

    audit.log_event(
        db,
        action="proposal_approved",
        ticket_id=ticket.id,
        actor=decided_by,
        details={
            "proposal_id": proposal.id,
            "action_type": proposal.action_type,
            "reason": reason,
        },
    )
    db.commit()
    db.refresh(proposal)
    return proposal


def reject_proposal(
    db: Session,
    proposal_id: int,
    *,
    decided_by: str,
    reason: str = "",
) -> ActionProposal:
    """Reject and halt a pending action proposal."""
    proposal = db.get(ActionProposal, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")

    if proposal.status != "pending_approval":
        raise ValueError(
            f"Proposal {proposal_id} is already decided (status: {proposal.status})"
        )

    proposal.status = "rejected"
    proposal.decided_by = decided_by
    proposal.decision_reason = reason
    proposal.decided_at = datetime.utcnow()
    proposal.execution_result = json.dumps(
        {"executed": False, "halted": True, "reason": reason}, ensure_ascii=False
    )

    audit.log_event(
        db,
        action="proposal_rejected",
        ticket_id=proposal.ticket_id,
        actor=decided_by,
        details={
            "proposal_id": proposal.id,
            "action_type": proposal.action_type,
            "reason": reason,
        },
    )
    db.commit()
    db.refresh(proposal)
    return proposal
