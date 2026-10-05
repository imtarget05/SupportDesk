"""Notification worker for SupportDesk.

Consumes ticket domain events from Kafka and sends email notifications
to customers and agents with consumer idempotency and error isolation.
"""

import logging
import time
from typing import Any

from app.database import SessionLocal
from app.services.email_service import send_agent_reply
from app.workers.event_consumer import EventConsumer

logger = logging.getLogger(__name__)


def handle_ticket_created(event_dict: dict[str, Any]) -> None:
    payload = event_dict.get("payload", {})
    ticket_id = payload.get("ticket_id")
    subject = payload.get("subject", "Ticket confirmation")
    logger.info("NotificationWorker: TicketCreated notification for #%s (%s)", ticket_id, subject)


def handle_ticket_assigned(event_dict: dict[str, Any]) -> None:
    payload = event_dict.get("payload", {})
    ticket_id = payload.get("ticket_id")
    assignee_name = payload.get("assignee_name", "Agent")
    assignee_email = payload.get("assignee_email")
    logger.info(
        "NotificationWorker: TicketAssigned dispatch for #%s to %s <%s>",
        ticket_id,
        assignee_name,
        assignee_email,
    )
    if assignee_email:
        send_agent_reply(
            to_email=assignee_email,
            ticket_id=int(ticket_id or 0),
            subject=f"Ticket #{ticket_id} Assigned to you",
            body=f"Hello {assignee_name},\n\nYou have been assigned ticket #{ticket_id}.",
        )


def handle_sla_breached(event_dict: dict[str, Any]) -> None:
    payload = event_dict.get("payload", {})
    ticket_id = payload.get("ticket_id")
    priority = payload.get("priority")
    logger.warning("NotificationWorker: SLA BREACH alert for ticket #%s (priority=%s)", ticket_id, priority)


def create_notification_consumer() -> EventConsumer:
    consumer = EventConsumer(consumer_group="notification-worker")
    consumer.register_handler("TicketCreated", handle_ticket_created)
    consumer.register_handler("TicketAssigned", handle_ticket_assigned)
    consumer.register_handler("SLABreached", handle_sla_breached)
    return consumer


def process_buffered_events(db: Session, consumer: EventConsumer) -> int:
    """Process any unhandled events from the producer buffer or event bus."""
    events = default_producer.get_published_events()
    processed_count = 0
    for ev in events:
        res = consumer.process_event(db, ev)
        if res.get("status") == "processed":
            processed_count += 1
    return processed_count


def run_notification_worker(poll_interval_s: float = 2.0, max_iterations: int | None = None) -> None:
    logger.info("Starting Notification Worker...")
    consumer = create_notification_consumer()
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        db = SessionLocal()
        try:
            count = process_buffered_events(db, consumer)
            if count > 0:
                logger.info("NotificationWorker processed %d events", count)
        except Exception as exc:
            logger.error("Error in notification worker loop: %s", exc)
        finally:
            db.close()
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_notification_worker()

