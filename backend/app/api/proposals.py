"""API endpoints for ActionProposals (Human-In-The-Loop gate).

Guarantees:
- Model cannot autonomously execute sensitive actions (refund, escalation, external reply).
- Actions are queued as `ActionProposal` in `pending_approval`.
- Only authorized users (agent or admin) can approve or reject proposals.
- Approval triggers action execution and logs audit events.
- Rejection halts the action and records the decision rationale in the audit trail.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, require_agent
from app.models import Ticket, User
from app.schemas.proposals import ProposalCreateIn, ProposalDecisionIn, ProposalOut
from app.services import hitl_service

router = APIRouter(prefix="/api/tickets", tags=["proposals"])


def _to_out(proposal) -> ProposalOut:
    payload = json.loads(proposal.payload) if proposal.payload else {}
    execution_result = (
        json.loads(proposal.execution_result) if proposal.execution_result else None
    )
    return ProposalOut(
        id=proposal.id,
        ticket_id=proposal.ticket_id,
        action_type=proposal.action_type,
        payload=payload,
        reasoning=proposal.reasoning,
        confidence=proposal.confidence,
        status=proposal.status,
        created_by=proposal.created_by,
        decided_by=proposal.decided_by,
        decision_reason=proposal.decision_reason,
        decided_at=proposal.decided_at,
        execution_result=execution_result,
        created_at=proposal.created_at,
    )


@router.get("/{ticket_id}/proposals", response_model=list[ProposalOut])
def list_ticket_proposals(
    ticket_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ProposalOut]:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
        )
    if user.role != "agent" and ticket.customer_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to ticket"
        )
    proposals = hitl_service.list_proposals(db, ticket_id=ticket_id)
    return [_to_out(p) for p in proposals]


@router.get("/{ticket_id}/proposals/{proposal_id}", response_model=ProposalOut)
def get_ticket_proposal(
    ticket_id: int,
    proposal_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProposalOut:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found"
        )
    if user.role != "agent" and ticket.customer_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to ticket"
        )
    proposal = hitl_service.get_proposal(db, proposal_id)
    if proposal is None or proposal.ticket_id != ticket_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found"
        )
    return _to_out(proposal)


@router.post(
    "/{ticket_id}/proposals/{proposal_id}/approve", response_model=ProposalOut
)
def approve_ticket_proposal(
    ticket_id: int,
    proposal_id: int,
    decision: ProposalDecisionIn | None = None,
    db: Session = Depends(get_db),
    agent: User = Depends(require_agent),
) -> ProposalOut:
    proposal = hitl_service.get_proposal(db, proposal_id)
    if proposal is None or proposal.ticket_id != ticket_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found"
        )
    if proposal.status != "pending_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Proposal is already decided ({proposal.status})",
        )
    reason = decision.reason if decision else ""
    try:
        updated = hitl_service.approve_proposal(
            db, proposal_id, decided_by=agent.email, reason=reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _to_out(updated)


@router.post(
    "/{ticket_id}/proposals/{proposal_id}/reject", response_model=ProposalOut
)
def reject_ticket_proposal(
    ticket_id: int,
    proposal_id: int,
    decision: ProposalDecisionIn | None = None,
    db: Session = Depends(get_db),
    agent: User = Depends(require_agent),
) -> ProposalOut:
    proposal = hitl_service.get_proposal(db, proposal_id)
    if proposal is None or proposal.ticket_id != ticket_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found"
        )
    if proposal.status != "pending_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Proposal is already decided ({proposal.status})",
        )
    reason = decision.reason if decision else ""
    try:
        updated = hitl_service.reject_proposal(
            db, proposal_id, decided_by=agent.email, reason=reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return _to_out(updated)
