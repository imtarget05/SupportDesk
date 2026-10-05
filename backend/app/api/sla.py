"""SLA and Outbox management endpoints.

Exposes live SLA metrics calculated directly from the database and allows
operators to trigger evaluation scans and outbox batch publication.
"""

from typing import Any
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_agent
from app.models.outbox import OutboxEvent
from app.models.user import User
from app.services.sla_service import evaluate_all_active_slas, get_sla_metrics
from app.workers.outbox_publisher import publish_outbox_batch

router = APIRouter(prefix="/sla", tags=["sla"])


@router.get("/metrics")
def read_sla_metrics(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_agent),
) -> dict[str, Any]:
    """Return live calculated SLA compliance and response metrics."""
    return get_sla_metrics(db)


@router.post("/evaluate")
def trigger_sla_evaluation(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_agent),
) -> dict[str, Any]:
    """Scan open tickets, detect warnings/breaches, and trigger auto-escalation."""
    summary = evaluate_all_active_slas(db)
    return {"message": "SLA evaluation completed", "summary": summary}


@router.get("/outbox/stats")
def read_outbox_stats(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_agent),
) -> dict[str, int]:
    """Return count of pending, published, and failed transactional outbox events."""
    pending = db.query(OutboxEvent).filter(OutboxEvent.status == "PENDING").count()
    published = db.query(OutboxEvent).filter(OutboxEvent.status == "PUBLISHED").count()
    failed = db.query(OutboxEvent).filter(OutboxEvent.status == "FAILED").count()
    total = db.query(OutboxEvent).count()
    return {
        "total": total,
        "pending": pending,
        "published": published,
        "failed": failed,
    }


@router.post("/outbox/publish-now")
def trigger_outbox_publish(
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_agent),
) -> dict[str, Any]:
    """Manually flush pending outbox events to Kafka / event bus."""
    dispatched = publish_outbox_batch(db)
    return {"message": "Outbox publish completed", "dispatched_count": dispatched}
