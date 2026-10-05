"""Transactional Outbox service.

Guarantees at-least-once delivery of domain events without distributed 2PC
transactions. Domain services write events directly into the database session
in the same transaction as their aggregate updates.
"""

from datetime import datetime
import json
import logging
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.outbox import OutboxEvent

logger = logging.getLogger(__name__)

MAX_OUTBOX_RETRIES = 5


def record_outbox_event(
    db: Session,
    *,
    event_type: str,
    aggregate_id: str | int,
    aggregate_type: str = "ticket",
    payload: dict[str, Any],
) -> OutboxEvent:
    """Record a domain event into the outbox table within the caller's transaction.

    Do NOT commit here — the event must be committed atomically with the
    enclosing aggregate mutation.
    """
    event_id = str(uuid.uuid4())
    event = OutboxEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        payload=json.dumps(payload, default=str),
        status="PENDING",
        retry_count=0,
    )
    db.add(event)
    return event


def get_pending_events(db: Session, limit: int = 50) -> list[OutboxEvent]:
    """Retrieve oldest pending outbox events for publishing."""
    return (
        db.query(OutboxEvent)
        .filter(OutboxEvent.status == "PENDING")
        .order_by(OutboxEvent.created_at.asc())
        .limit(limit)
        .all()
    )


def mark_event_published(db: Session, event_id: str) -> None:
    """Mark an outbox event as successfully published."""
    event = db.query(OutboxEvent).filter(OutboxEvent.event_id == event_id).first()
    if event is not None:
        event.status = "PUBLISHED"
        event.published_at = datetime.utcnow()
        event.error_message = None
        db.commit()


def mark_event_failed(db: Session, event_id: str, error: str) -> None:
    """Increment retry count and mark failed if retry limit is exceeded."""
    event = db.query(OutboxEvent).filter(OutboxEvent.event_id == event_id).first()
    if event is not None:
        event.retry_count += 1
        event.error_message = error[:1000]
        if event.retry_count >= MAX_OUTBOX_RETRIES:
            event.status = "FAILED"
        db.commit()
