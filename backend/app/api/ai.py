"""AI endpoints (agent-only, suggestion-only).

- analyze: validated AI output updates ticket triage fields + logs ai_predictions.
  Any provider failure returns 502 and leaves the ticket untouched.
- suggest: returns a draft reply. NEVER sends it — the agent must post a message
  explicitly through the normal message endpoint.
- similar: resolved/closed tickets ranked by embedding cosine similarity.
- workflow: full ticket pipeline (classify -> retrieve -> draft -> confidence check).
- knowledge: knowledge base statistics and management.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.deps import require_agent
from app.enums import TicketStatus
from app.models import AIPrediction, Ticket
from app.schemas import (
    AgentRunOut,
    AISuggestionOut,
    SimilarTicketsOut,
    TicketOut,
    WorkflowRunOut,
)
from app.services import ai_service, retrieval_service, ticket_service
from app.services.graph_workflow import get_graph
from app.services.knowledge_base import get_knowledge_base
from app.services.tool_agent import ToolCallingAgent
from app.tools import build_registry
from app.workflows.definitions import TicketWorkflowInput
from app.workflows.local_runner import get_runner

router = APIRouter(prefix="/api/tickets", tags=["ai"])


def _ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    ticket = ticket_service.get_ticket_or_none(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ticket is closed")
    return ticket


def _ai_error(exc: ai_service.AIProviderError) -> HTTPException:
    """Map an AI-layer failure to an honest status code.

    Budget refusals are not upstream failures: a spent cap is throttling
    (429, retry next month) and an unreadable spend is "we cannot tell" (503).
    Anything else stays 502 — the upstream provider or our parsing failed.
    """
    if isinstance(exc, ai_service.BudgetExceededError):
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if exc.reason == "spend_unavailable"
            else status.HTTP_429_TOO_MANY_REQUESTS
        )
        return HTTPException(status_code=code, detail=str(exc))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/{ticket_id}/ai/analyze", response_model=TicketOut)
def analyze(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> TicketOut:
    ticket = _ticket_or_404(db, ticket_id)
    try:
        result = ai_service.analyze_ticket(
            ticket.subject, ticket.description, ticket_id=ticket.id
        )
    except ai_service.AIProviderError as exc:
        raise _ai_error(exc)

    # Persist only after validation succeeded.
    ticket.category = result.category.value
    ticket.priority = result.priority.value
    ticket.ai_summary = result.summary
    ticket.ai_confidence = result.confidence
    db.add(
        AIPrediction(
            ticket_id=ticket.id,
            model=settings.ai_provider,
            category=result.category.value,
            priority=result.priority.value,
            confidence=result.confidence,
        )
    )
    db.commit()
    db.refresh(ticket)
    retrieval_service.ensure_embedding(db, ticket)
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/ai/suggest", response_model=AISuggestionOut)
def suggest(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> AISuggestionOut:
    ticket = _ticket_or_404(db, ticket_id)
    similar = retrieval_service.find_similar_tickets(db, ticket)
    thread = "\n".join(f"{m.sender.role}: {m.content}" for m in ticket.messages)
    try:
        draft = ai_service.suggest_response(
            ticket.subject, ticket.description, thread, ticket_id=ticket.id
        )
    except ai_service.AIProviderError as exc:
        raise _ai_error(exc)
    return AISuggestionOut(response=draft, based_on_similar=[s["ticket_id"] for s in similar])


@router.post("/{ticket_id}/ai/agent", response_model=AgentRunOut)
def agent_run(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> AgentRunOut:
    """Run the tool-calling agent loop and return its draft plus what it did.

    Suggestion-only: the loop may call read-only tools, but it never sends a
    message or changes ticket state. A run that trips a guardrail returns 502
    with the reason, and the draft is withheld.
    """
    ticket = _ticket_or_404(db, ticket_id)
    thread = "\n".join(f"{m.sender.role}: {m.content}" for m in ticket.messages)
    registry = build_registry(db)
    runner = ToolCallingAgent(registry)

    result = runner.run(ticket.subject, ticket.description, thread)
    if result.error and not result.draft:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.error)

    return AgentRunOut(
        draft=result.draft,
        stopped_reason=result.stopped_reason,
        steps=len(result.steps),
        tool_calls=result.tool_calls,
    )


@router.post("/{ticket_id}/ai/workflow/run", response_model=WorkflowRunOut)
def start_workflow_run(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> WorkflowRunOut:
    """Start the durable ticket workflow for a ticket.

    Runs the stages in order and stops before `send` when the draft is
    high-impact, returning the workflow id so the agent can approve or reject it
    through `/ai/workflow/{workflow_id}/decision`. Nothing is sent by this
    endpoint.
    """
    ticket = _ticket_or_404(db, ticket_id)
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
    return _workflow_out(run)


@router.post("/{ticket_id}/ai/workflow/{workflow_id}/decision", response_model=WorkflowRunOut)
def decide_workflow_run(
    ticket_id: int,
    workflow_id: str,
    approved: bool,
    decided_by: str = "agent",
    reason: str = "",
    _agent=Depends(require_agent),
) -> WorkflowRunOut:
    """Approve or reject a workflow run that is waiting on a human."""
    runner = get_runner()
    try:
        run = runner.decide(workflow_id, approved=approved, decided_by=decided_by, reason=reason)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown workflow run"
        )
    if run.result.ticket_id != ticket_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Workflow run is for another ticket"
        )
    return _workflow_out(run)


@router.get("/{ticket_id}/ai/workflow/{workflow_id}", response_model=WorkflowRunOut)
def get_workflow_run(
    ticket_id: int,
    workflow_id: str,
    _agent=Depends(require_agent),
) -> WorkflowRunOut:
    """Current state of a workflow run, including whether it awaits approval."""
    run = get_runner().get(workflow_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown workflow run"
        )
    if run.result.ticket_id != ticket_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Workflow run is for another ticket"
        )
    return _workflow_out(run)


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
        events=run.events,
        error=run.result.error,
    )


@router.get("/{ticket_id}/similar", response_model=SimilarTicketsOut)
def similar(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> SimilarTicketsOut:
    ticket = _ticket_or_404(db, ticket_id)
    return SimilarTicketsOut(items=retrieval_service.find_similar_tickets(db, ticket))


@router.post("/{ticket_id}/ai/workflow")
def workflow(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> dict:
    """Run the full ticket processing pipeline for a ticket.

    This endpoint orchestrates:
    1. Ticket classification (category, priority, summary, confidence)
    2. Context retrieval from knowledge base (RAG)
    3. Response drafting with evidence
    4. Confidence check and routing decision

    The workflow NEVER sends a response — it returns a draft and routing
    decision for the agent to review.
    """
    ticket = _ticket_or_404(db, ticket_id)
    thread = "\n".join(f"{m.sender.role}: {m.content}" for m in ticket.messages)

    graph = get_graph()
    result = graph.process_ticket(
        ticket_id=ticket.id,
        subject=ticket.subject,
        description=ticket.description,
        thread=thread,
    )

    # Update ticket with classification results (if successful)
    if not result.error:
        ticket.category = result.category
        ticket.priority = result.priority
        ticket.ai_summary = result.summary
        ticket.ai_confidence = result.confidence
        db.add(
            AIPrediction(
                ticket_id=ticket.id,
                model=settings.ai_provider,
                category=result.category,
                priority=result.priority,
                confidence=result.confidence,
            )
        )
        db.commit()
        db.refresh(ticket)
        retrieval_service.ensure_embedding(db, ticket)

    return {
        "workflow_id": result.workflow_id,
        "ticket_id": result.ticket_id,
        "stage": result.stage.value,
        "category": result.category,
        "priority": result.priority,
        "summary": result.summary,
        "confidence": result.confidence,
        "draft": result.draft,
        "requires_human_review": result.requires_human_review,
        "requires_approval": result.requires_approval,
        "review_reason": result.review_reason,
        "evidence": result.evidence,
        "audit_log": result.audit_log,
        "error": result.error,
    }


@router.get("/ai/knowledge/stats")
def knowledge_stats(
    agent=Depends(require_agent),
) -> dict:
    """Get knowledge base statistics."""
    kb = get_knowledge_base()
    return kb.get_stats()


@router.post("/ai/knowledge/ingest")
def knowledge_ingest(
    agent=Depends(require_agent),
) -> dict:
    """Trigger knowledge base ingestion."""
    kb = get_knowledge_base()
    count = kb.ingest(force=True)
    return {"documents_ingested": count, **kb.get_stats()}
