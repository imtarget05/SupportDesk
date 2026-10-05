"""SLA Engine for SupportDesk.

Calculates priority-based SLA targets, detects warnings (80% threshold),
flags breaches, triggers escalations, records audit events, and exposes
verifiable metrics from the database.
"""

from datetime import datetime, timedelta
import json
import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.enums import TicketPriority, TicketStatus
from app.models.audit import AuditEvent
from app.models.ticket import Ticket
from app.services.outbox import record_outbox_event

logger = logging.getLogger(__name__)

# SLA target durations in minutes
FIRST_RESPONSE_TARGETS_MINUTES: dict[str, int] = {
    TicketPriority.URGENT.value: 60,     # 1 hour
    TicketPriority.HIGH.value: 240,     # 4 hours
    TicketPriority.NORMAL.value: 480,   # 8 hours
    TicketPriority.LOW.value: 1440,     # 24 hours
}

RESOLUTION_TARGETS_MINUTES: dict[str, int] = {
    TicketPriority.URGENT.value: 240,   # 4 hours
    TicketPriority.HIGH.value: 480,    # 8 hours
    TicketPriority.NORMAL.value: 1440,  # 24 hours
    TicketPriority.LOW.value: 2880,     # 48 hours
}


def calculate_sla_due_dates(
    priority: str,
    base_time: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Calculate first_response_due_at and resolution_due_at based on priority."""
    start = base_time or datetime.utcnow()
    resp_minutes = FIRST_RESPONSE_TARGETS_MINUTES.get(
        priority, FIRST_RESPONSE_TARGETS_MINUTES[TicketPriority.NORMAL.value]
    )
    res_minutes = RESOLUTION_TARGETS_MINUTES.get(
        priority, RESOLUTION_TARGETS_MINUTES[TicketPriority.NORMAL.value]
    )

    first_response_due = start + timedelta(minutes=resp_minutes)
    resolution_due = start + timedelta(minutes=res_minutes)
    return first_response_due, resolution_due


def check_and_update_ticket_sla(
    db: Session,
    ticket: Ticket,
    current_time: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Check SLA status for a single ticket and emit events on warning or breach.

    Returns dict with update flags.
    """
    now = current_time or datetime.utcnow()
    result = {"status_changed": False, "escalated": False, "warning": False, "breached": False}

    if not ticket.resolution_due_at:
        return result

    # Check if resolved
    is_resolved = ticket.status in (TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value)
    if is_resolved:
        if ticket.resolved_at and ticket.resolved_at > ticket.resolution_due_at:
            if ticket.sla_status != "BREACHED":
                ticket.sla_status = "BREACHED"
                result["status_changed"] = True
        return result

    # Check resolution deadline breach
    is_breached = now > ticket.resolution_due_at
    # Check first response breach (if not responded yet)
    if not is_breached and not ticket.first_responded_at and ticket.first_response_due_at:
        if now > ticket.first_response_due_at:
            is_breached = True

    if is_breached:
        if ticket.sla_status != "BREACHED":
            ticket.sla_status = "BREACHED"
            result["breached"] = True
            result["status_changed"] = True

            # Emit SLABreached outbox event
            record_outbox_event(
                db,
                event_type="SLABreached",
                aggregate_id=ticket.id,
                payload={
                    "ticket_id": ticket.id,
                    "priority": ticket.priority,
                    "resolution_due_at": ticket.resolution_due_at.isoformat(),
                    "breached_at": now.isoformat(),
                },
            )

            # Record audit event
            db.add(
                AuditEvent(
                    ticket_id=ticket.id,
                    action="sla.breached",
                    actor="system.sla_engine",
                    details=json.dumps(
                        {"deadline": ticket.resolution_due_at.isoformat(), "breached_at": now.isoformat()}
                    ),
                )
            )

            # Trigger automated escalation if not already escalated
            if ticket.escalation_level == 0:
                ticket.escalation_level = 1
                result["escalated"] = True

                record_outbox_event(
                    db,
                    event_type="TicketEscalated",
                    aggregate_id=ticket.id,
                    payload={
                        "ticket_id": ticket.id,
                        "escalation_level": 1,
                        "reason": "SLA breach auto-escalation",
                        "escalated_at": now.isoformat(),
                    },
                )

                db.add(
                    AuditEvent(
                        ticket_id=ticket.id,
                        action="ticket.escalated",
                        actor="system.sla_engine",
                        details=json.dumps({"escalation_level": 1, "reason": "SLA breach auto-escalation"}),
                    )
                )

    else:
        # Check 80% SLA Warning threshold
        created = ticket.created_at or (ticket.resolution_due_at - timedelta(minutes=RESOLUTION_TARGETS_MINUTES.get(ticket.priority, 1440)))
        total_seconds = (ticket.resolution_due_at - created).total_seconds()
        elapsed_seconds = (now - created).total_seconds()
        ratio = elapsed_seconds / total_seconds if total_seconds > 0 else 0.0

        if ratio >= 0.8 and ticket.sla_status == "WITHIN_SLA":
            ticket.sla_status = "WARNING"
            result["warning"] = True
            result["status_changed"] = True

            record_outbox_event(
                db,
                event_type="SLAWarningTriggered",
                aggregate_id=ticket.id,
                payload={
                    "ticket_id": ticket.id,
                    "priority": ticket.priority,
                    "ratio": round(ratio, 2),
                    "resolution_due_at": ticket.resolution_due_at.isoformat(),
                },
            )

            db.add(
                AuditEvent(
                    ticket_id=ticket.id,
                    action="sla.warning",
                    actor="system.sla_engine",
                    details=json.dumps(
                        {"ratio": round(ratio, 2), "resolution_due_at": ticket.resolution_due_at.isoformat()}
                    ),
                )
            )

    if commit and (result["status_changed"] or result["escalated"]):
        db.commit()

    return result


def evaluate_all_active_slas(
    db: Session,
    current_time: datetime | None = None,
) -> dict[str, int]:
    """Scan all active tickets, check deadlines, update status, and commit changes."""
    active_statuses = (
        TicketStatus.OPEN.value,
        TicketStatus.IN_PROGRESS.value,
        TicketStatus.WAITING.value,
    )
    tickets = (
        db.query(Ticket)
        .filter(Ticket.status.in_(active_statuses))
        .all()
    )

    summary = {"evaluated": len(tickets), "breached": 0, "warnings": 0, "escalated": 0}
    for ticket in tickets:
        res = check_and_update_ticket_sla(db, ticket, current_time=current_time, commit=False)
        if res["breached"]:
            summary["breached"] += 1
        if res["warning"]:
            summary["warnings"] += 1
        if res["escalated"]:
            summary["escalated"] += 1

    db.commit()
    return summary


def get_sla_metrics(db: Session) -> dict[str, Any]:
    """Query live PostgreSQL/SQLite database to compute SLA metrics."""
    total_tickets = db.query(Ticket).count()

    resolved_tickets = (
        db.query(Ticket)
        .filter(Ticket.status.in_([TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value]))
        .all()
    )
    total_resolved = len(resolved_tickets)

    resolved_within_sla = sum(
        1 for t in resolved_tickets
        if t.resolution_due_at and t.resolved_at and t.resolved_at <= t.resolution_due_at
    )
    # Tickets resolved without resolution_due_at count as compliant
    resolved_within_sla += sum(1 for t in resolved_tickets if not t.resolution_due_at)

    compliance_rate = (
        round((resolved_within_sla / total_resolved) * 100.0, 2)
        if total_resolved > 0
        else 100.0
    )

    active_breaches = (
        db.query(Ticket)
        .filter(Ticket.sla_status == "BREACHED")
        .count()
    )

    active_warnings = (
        db.query(Ticket)
        .filter(Ticket.sla_status == "WARNING")
        .count()
    )

    escalated_count = (
        db.query(Ticket)
        .filter(Ticket.escalation_level > 0)
        .count()
    )

    # Calculate average first response and resolution time in minutes
    first_response_diffs = []
    for t in db.query(Ticket).filter(Ticket.first_responded_at.is_not(None)).all():
        if t.created_at and t.first_responded_at:
            diff = (t.first_responded_at - t.created_at).total_seconds() / 60.0
            first_response_diffs.append(diff)

    resolution_diffs = []
    for t in resolved_tickets:
        if t.created_at and t.resolved_at:
            diff = (t.resolved_at - t.created_at).total_seconds() / 60.0
            resolution_diffs.append(diff)

    avg_first_response = (
        round(sum(first_response_diffs) / len(first_response_diffs), 1)
        if first_response_diffs
        else 0.0
    )
    avg_resolution = (
        round(sum(resolution_diffs) / len(resolution_diffs), 1)
        if resolution_diffs
        else 0.0
    )

    # Priority breakdown
    by_priority = {}
    for p in TicketPriority:
        p_total = db.query(Ticket).filter(Ticket.priority == p.value).count()
        p_breached = db.query(Ticket).filter(Ticket.priority == p.value, Ticket.sla_status == "BREACHED").count()
        by_priority[p.value] = {
            "total": p_total,
            "breached": p_breached,
            "compliance_rate": round(((p_total - p_breached) / p_total) * 100.0, 2) if p_total > 0 else 100.0,
        }

    return {
        "total_tickets": total_tickets,
        "resolved_tickets": total_resolved,
        "compliance_rate_percent": compliance_rate,
        "active_breaches": active_breaches,
        "active_warnings": active_warnings,
        "escalated_tickets": escalated_count,
        "avg_first_response_time_minutes": avg_first_response,
        "avg_resolution_time_minutes": avg_resolution,
        "by_priority": by_priority,
    }
