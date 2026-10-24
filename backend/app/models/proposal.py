from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin


class ActionProposal(TimestampMixin, Base):
    """An action proposed by an AI agent that requires Human-In-The-Loop approval.

    The model CANNOT execute sensitive actions (refund, escalation, outbound
    customer reply) autonomously. Instead, it creates an `ActionProposal` in
    `pending_approval` state. A human operator reviews, then approves or
    rejects the proposal through the authorized endpoint.
    """

    __tablename__ = "action_proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id"), index=True, nullable=False
    )
    action_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # "refund", "escalate", "external_reply"
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending_approval", index=True
    )  # "pending_approval", "approved", "rejected"
    created_by: Mapped[str] = mapped_column(
        String(100), nullable=False, default="ai_agent"
    )
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    execution_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    ticket = relationship("Ticket", foreign_keys=[ticket_id])
