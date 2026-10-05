"""Durable audit trail for support operations.

Audit events are rows in the ``audit_events`` table (see
``app/models/audit.py``), written in the same database transaction as the
domain change they describe — never process memory. The trail therefore
survives process restarts and is queryable per ticket.

Secrets policy: ``details`` must never carry tokens, passwords or API keys.
Keys named ``token``, ``password``, ``secret`` (case-insensitive, including
``*_token`` / ``*_secret`` variants) are stripped before persistence.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditEvent

logger = logging.getLogger(__name__)

_SECRET_SUFFIXES = ("token", "password", "secret", "api_key", "apikey")


def _scrub_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Remove secret-bearing keys from audit details."""
    cleaned: dict[str, Any] = {}
    for key, value in (details or {}).items():
        lowered = str(key).lower()
        if lowered in _SECRET_SUFFIXES or any(
            lowered == suffix or lowered.endswith("_" + suffix)
            for suffix in _SECRET_SUFFIXES
        ):
            continue
        cleaned[key] = value
    return cleaned


def log_event(
    db: Session,
    *,
    action: str,
    ticket_id: int,
    workflow_id: str = "manual",
    stage: str = "",
    details: dict[str, Any] | None = None,
    actor: str = "system",
) -> AuditEvent:
    """Persist one audit event. The caller owns commit/rollback."""
    event = AuditEvent(
        ticket_id=ticket_id,
        action=action,
        workflow_id=workflow_id,
        stage=stage,
        actor=actor,
        details=json.dumps(_scrub_details(details), ensure_ascii=False),
    )
    db.add(event)
    db.flush()
    logger.info(
        "AUDIT: ticket=%d action=%s stage=%s actor=%s",
        ticket_id, action, stage, actor,
    )
    return event


def list_events(
    db: Session,
    *,
    ticket_id: int,
    action: str | None = None,
    limit: int = 100,
) -> list[AuditEvent]:
    """Audit events for a ticket, oldest first (stable chronological order)."""
    query = db.query(AuditEvent).filter(AuditEvent.ticket_id == ticket_id)
    if action is not None:
        query = query.filter(AuditEvent.action == action)
    return query.order_by(AuditEvent.id.asc()).limit(limit).all()


def event_to_dict(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "timestamp": event.created_at.isoformat() if event.created_at else None,
        "action": event.action,
        "ticket_id": event.ticket_id,
        "workflow_id": event.workflow_id,
        "stage": event.stage,
        "details": json.loads(event.details) if event.details else {},
        "actor": event.actor,
    }
