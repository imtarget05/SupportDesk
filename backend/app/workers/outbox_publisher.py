"""Outbox publisher worker.

Polls pending events from the outbox table and dispatches them to Kafka.
Upon successful dispatch, marks the outbox event as PUBLISHED.
"""

import logging
import time
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.services.kafka_producer import KafkaEventProducer, default_producer
from app.services.outbox import (
    get_pending_events,
    mark_event_failed,
    mark_event_published,
)

logger = logging.getLogger(__name__)


def publish_outbox_batch(
    db: Session,
    producer: KafkaEventProducer | None = None,
    batch_size: int = 50,
) -> int:
    """Process a single batch of pending outbox events.

    Returns the count of successfully dispatched events.
    """
    prod = producer or default_producer
    pending = get_pending_events(db, limit=batch_size)
    if not pending:
        return 0

    dispatched = 0
    for event in pending:
        try:
            success = prod.publish_event(
                event_type=event.event_type,
                aggregate_id=event.aggregate_id,
                payload=event.payload,
                event_id=event.event_id,
            )
            if success:
                mark_event_published(db, event.event_id)
                dispatched += 1
            else:
                mark_event_failed(db, event.event_id, "Producer returned failure")
        except Exception as exc:
            logger.error("Error publishing outbox event %s: %s", event.event_id, exc)
            mark_event_failed(db, event.event_id, str(exc))

    return dispatched


def run_outbox_publisher_loop(poll_interval_s: float = 1.0, max_iterations: int | None = None) -> None:
    """Continuously poll and publish outbox events in a loop."""
    logger.info("Starting Outbox Publisher loop (interval: %ss)", poll_interval_s)
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        db = SessionLocal()
        try:
            count = publish_outbox_batch(db)
            if count > 0:
                logger.info("Published %d outbox events", count)
        except Exception as exc:
            logger.error("Error in outbox publisher loop: %s", exc)
        finally:
            db.close()
        time.sleep(poll_interval_s)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_outbox_publisher_loop()
