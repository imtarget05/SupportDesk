"""Idempotent Event Consumer with Dead Letter Queue (DLQ) support.

Ensures exactly-once side-effect execution by checking the processed_events
table before processing any Kafka event. Failures are retried up to MAX_RETRIES
before being routed to the DLQ topic.
"""

from datetime import datetime
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.outbox import ProcessedEvent
from app.services.kafka_producer import KafkaEventProducer, default_producer

logger = logging.getLogger(__name__)

MAX_CONSUMER_RETRIES = 3


class EventConsumer:
    def __init__(
        self,
        consumer_group: str = "supportdesk-workers",
        producer: KafkaEventProducer | None = None,
    ) -> None:
        self.consumer_group = consumer_group
        self.producer = producer or default_producer
        self._handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def register_handler(
        self, event_type: str, handler: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a callback for a specific event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    def is_already_processed(self, db: Session, event_id: str) -> bool:
        """Check if this event was already processed by this consumer group."""
        return (
            db.query(ProcessedEvent)
            .filter(
                ProcessedEvent.event_id == event_id,
                ProcessedEvent.consumer_group == self.consumer_group,
            )
            .first()
            is not None
        )

    def mark_processed(self, db: Session, event_id: str) -> None:
        """Record that this event has been processed to prevent duplicates."""
        record = ProcessedEvent(
            event_id=event_id,
            consumer_group=self.consumer_group,
            processed_at=datetime.utcnow(),
        )
        db.add(record)
        db.commit()

    def process_event(
        self,
        db: Session,
        event_dict: dict[str, Any],
        retry_count: int = 0,
    ) -> dict[str, Any]:
        """Process a single event with idempotency check, handlers, and DLQ routing."""
        event_id = event_dict.get("event_id")
        event_type = event_dict.get("event_type", "unknown")
        aggregate_id = str(event_dict.get("aggregate_id", ""))

        if not event_id:
            logger.warning("Event missing event_id, rejecting to DLQ")
            self.route_to_dlq(event_dict, "Missing event_id")
            return {"status": "dlq", "reason": "missing_event_id"}

        # 1. Idempotency Check
        if self.is_already_processed(db, event_id):
            logger.info(
                "Idempotency filter: event %s already processed by %s, skipping",
                event_id,
                self.consumer_group,
            )
            return {"status": "skipped", "reason": "already_processed"}

        # 2. Execute registered handlers
        handlers = self._handlers.get(event_type, [])
        try:
            for handler in handlers:
                handler(event_dict)

            # 3. Mark processed
            self.mark_processed(db, event_id)
            logger.info("Successfully processed event %s (%s)", event_id, event_type)
            return {"status": "processed", "event_id": event_id}

        except Exception as exc:
            logger.error(
                "Handler error processing event %s (attempt %d/%d): %s",
                event_id,
                retry_count + 1,
                MAX_CONSUMER_RETRIES,
                exc,
            )
            if retry_count + 1 < MAX_CONSUMER_RETRIES:
                self.route_to_retry(event_dict, retry_count + 1)
                return {
                    "status": "retry",
                    "attempt": retry_count + 1,
                    "error": str(exc),
                }
            else:
                self.route_to_dlq(event_dict, str(exc))
                return {"status": "dlq", "error": str(exc)}

    def route_to_retry(self, event_dict: dict[str, Any], attempt: int) -> None:
        """Publish failing event to retry topic."""
        retry_payload = dict(event_dict)
        retry_payload["retry_count"] = attempt
        self.producer.publish_event(
            event_type=event_dict.get("event_type", "unknown"),
            aggregate_id=str(event_dict.get("aggregate_id", "")),
            payload=retry_payload,
            topic=self.producer.topic_retry,
            event_id=event_dict.get("event_id"),
        )
        logger.warning(
            "Event %s routed to retry topic (attempt %d)",
            event_dict.get("event_id"),
            attempt,
        )

    def route_to_dlq(self, event_dict: dict[str, Any], error_reason: str) -> None:
        """Publish exhausted event to Dead Letter Queue."""
        dlq_payload = dict(event_dict)
        dlq_payload["error_reason"] = error_reason
        dlq_payload["dead_lettered_at"] = datetime.utcnow().isoformat()
        self.producer.publish_event(
            event_type=event_dict.get("event_type", "unknown"),
            aggregate_id=str(event_dict.get("aggregate_id", "")),
            payload=dlq_payload,
            topic=self.producer.topic_dlq,
            event_id=event_dict.get("event_id"),
        )
        logger.error(
            "Event %s dead-lettered to %s: %s",
            event_dict.get("event_id"),
            self.producer.topic_dlq,
            error_reason,
        )
