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
from app.schemas import AISuggestionOut, SimilarTicketsOut, TicketOut
from app.services import ai_service, retrieval_service, ticket_service
from app.services.graph_workflow import get_graph
from app.services.knowledge_base import get_knowledge_base

router = APIRouter(prefix="/api/tickets", tags=["ai"])


def _ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    ticket = ticket_service.get_ticket_or_none(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if ticket.status == TicketStatus.CLOSED.value:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ticket is closed")
    return ticket


@router.post("/{ticket_id}/ai/analyze", response_model=TicketOut)
def analyze(
    ticket_id: int,
    db: Session = Depends(get_db),
    agent=Depends(require_agent),
) -> TicketOut:
    ticket = _ticket_or_404(db, ticket_id)
    try:
        result = ai_service.analyze_ticket(ticket.subject, ticket.description)
    except ai_service.AIProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

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
        draft = ai_service.suggest_response(ticket.subject, ticket.description, thread)
    except ai_service.AIProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return AISuggestionOut(response=draft, based_on_similar=[s["ticket_id"] for s in similar])


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
