from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class AuditEvent(TimestampMixin, Base):
    """Durable audit record for ticket-domain actions.

    Written to the application database (not process memory), so the trail
    survives restarts. ``details`` is a JSON blob and must never contain
    secrets (tokens, passwords) — the service layer strips those keys.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id"), index=True, nullable=False
    )
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    __table_args__ = (
        Index("ix_audit_events_ticket_created", "ticket_id", "created_at"),
    )
