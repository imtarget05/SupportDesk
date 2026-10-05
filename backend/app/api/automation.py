from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_agent
from app.enums import TicketStatus
from app.models import Ticket, User
from app.schemas import WorkflowRunOut
from app.services import ticket_service
from app.services import audit as audit_svc
from app.workflows.definitions import TicketWorkflowInput
from app.workflows.local_runner import get_runner

router = APIRouter(prefix="/api/automation", tags=["automation"])


class AutomationJobRequest(BaseModel):
    ticket_id: int
    workflow_name: str | None = "ticket_workflow"
    parameters: dict[str, Any] | None = None


def _workflow_out(run) -> WorkflowRunOut:
    return WorkflowRunOut(
        workflow_id=run.workflow_id,
        ticket_id=run.result.ticket_id,
        category=run.result.category,
        priority=run.result.priority,
        summary=run.result.summary,
        confidence=run.result.confidence,
        draft=run.result.draft,
        requires_approval=run.result.requires_approval,
        approved=run.result.approved,
        sent=run.result.sent,
        review_reason=run.result.review_reason,
    )


@router.post("/jobs", response_model=WorkflowRunOut)
def run_automation_job(
    body: AutomationJobRequest,
    db: Session = Depends(get_db),
    agent: User = Depends(require_agent),
) -> WorkflowRunOut:
    """Execute the ticket workflow **synchronously** and return its result.

    There is deliberately no background queue here: the local runner executes
    classify → retrieve → draft inline and this endpoint returns the
    completed outcome. The ``/jobs`` path is kept for API compatibility, but
    callers must treat it as a synchronous automation execution — there is no
    job identity to poll and no PENDING/RUNNING lifecycle. If durable async
    execution is ever needed, it must be introduced as a real persisted job
    model, not by re-labelling this call.
    """
    ticket = ticket_service.get_ticket_or_none(db, body.ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ticket is closed")

    thread = "\n".join(f"{m.sender.role}: {m.content}" for m in ticket.messages)
    runner = get_runner()
    run = runner.start(
        TicketWorkflowInput(
            ticket_id=ticket.id,
            subject=ticket.subject,
            description=ticket.description,
            thread=thread,
        )
    )
    audit_svc.log_event(
        db,
        action="automation.job.start",
        ticket_id=ticket.id,
        workflow_id=run.workflow_id,
        stage="initiation",
        details={
            "workflow_name": body.workflow_name,
            "parameters": body.parameters,
            "execution": "synchronous",
        },
        actor=agent.name,
    )
    db.commit()
    return _workflow_out(run)
