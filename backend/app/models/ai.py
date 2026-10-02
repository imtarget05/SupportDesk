"""AI-related tables: embeddings, raw predictions, evaluation records."""

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class TicketEmbedding(Base):
    __tablename__ = "ticket_embeddings"

    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), primary_key=True)
    # JSON-encoded list[float]; vector search runs in Python at MVP scale.
    embedding: Mapped[str] = mapped_column(Text, nullable=False)
    # Which embedder produced this vector, and how wide it is. Stored because
    # cosine similarity across vectors of different dimensions is meaningless:
    # mixing a 128-dim bag-of-words vector with a 384-dim MiniLM vector would
    # silently produce a wrong number rather than an error.
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="bow-sha256")
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=128)


class AIPrediction(TimestampMixin, Base):
    __tablename__ = "ai_predictions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    # Raw LLM output kept for debugging/evaluation.
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)


class AIEvaluation(TimestampMixin, Base):
    __tablename__ = "ai_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True, nullable=False)
    expected_category: Mapped[str] = mapped_column(String(30), nullable=False)
    predicted_category: Mapped[str] = mapped_column(String(30), nullable=False)
    correct: Mapped[bool] = mapped_column(Boolean, nullable=False)


class AICallTrace(TimestampMixin, Base):
    """One row per LLM call: what was sent, what it cost, and how it failed.

    The in-process counters in ``services/metrics.py`` are lost on restart and
    cannot be broken down per model or per operation. This table is the durable
    record that answers "what does this feature cost, and where does it fail",
    which is the question an on-call engineer actually has.
    """

    __tablename__ = "ai_call_traces"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # correlate the LLM call with the ticket it was serving, when there is one.
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id"), index=True, nullable=True
    )
    operation: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(100), index=True, nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NULL when the model is not in the pricing table. Never defaulted to 0,
    # because "we could not price this" and "this call was free" are different.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pricing_table_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Confidence for triage calls; NULL for generation calls.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

