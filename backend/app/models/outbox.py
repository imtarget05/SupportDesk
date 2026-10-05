from datetime import datetime
from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class OutboxEvent(TimestampMixin, Base):
    """Transactional Outbox event.

    Persisted in the exact same database transaction as the domain aggregate
    (Ticket, Message, etc.), guaranteeing at-least-once delivery to Kafka
    without dual-write inconsistencies.
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False, default="ticket")
    aggregate_id: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False, default="PENDING")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_outbox_events_status_created", "status", "created_at"),
    )


class ProcessedEvent(Base):
    """Consumer idempotency tracking.

    Prevents duplicate side-effects (e.g. duplicate email, double SLA escalation)
    when Kafka redelivers messages under network partitions or consumer restarts.
    """

    __tablename__ = "processed_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    consumer_group: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_processed_events_dedup", "event_id", "consumer_group", unique=True),
    )
