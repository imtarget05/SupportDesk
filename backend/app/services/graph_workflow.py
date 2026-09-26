"""Ticket processing pipeline for AI-assisted support.

This module implements the control plane for the AI-assisted support workflow. It is a
straight-line sequence of stage methods over a shared `TicketState`; no external
graph/orchestration framework is involved.

Stages:
    classify_ticket -> retrieve_context -> draft_answer -> confidence_check
                                                              |
                                          -------------------+-------------------
                                          V                   V                   V
                                     auto-send          human-review       approval-required
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.config import settings
from app.services import ai_service, guardrails
from app.services.langchain_agent import get_agent
from app.services.knowledge_base import get_knowledge_base

logger = logging.getLogger(__name__)


class WorkflowStage(str, Enum):
    """Stages in the ticket processing workflow."""
    START = "start"
    CLASSIFY = "classify"
    RETRIEVE = "retrieve"
    DRAFT = "draft"
    CONFIDENCE_CHECK = "confidence_check"
    AUTO_SEND = "auto_send"
    HUMAN_REVIEW = "human_review"
    APPROVAL_REQUIRED = "approval_required"
    SEND = "send"
    END = "end"


class ConfidenceLevel(str, Enum):
    """Confidence levels for routing decisions."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Thresholds for routing decisions
HIGH_CONFIDENCE_THRESHOLD = 0.85
LOW_CONFIDENCE_THRESHOLD = 0.60


@dataclass
class TicketState:
    """State for a ticket processing workflow."""
    ticket_id: int
    subject: str
    description: str
    thread: str = ""
    category: str = "unknown"
    priority: str = "normal"
    summary: str = ""
    confidence: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    draft: str = ""
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    stage: WorkflowStage = WorkflowStage.START
    requires_human_review: bool = False
    requires_approval: bool = False
    review_reason: str = ""
    audit_log: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    workflow_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: float = field(default_factory=time.time)

    def log_action(self, action: str, details: dict[str, Any] | None = None) -> None:
        """Add an entry to the audit log."""
        entry = {
            "action": action,
            "stage": self.stage.value,
            "timestamp": time.time(),
            "workflow_id": self.workflow_id,
        }
        if details:
            entry["details"] = details
        self.audit_log.append(entry)


@dataclass
class WorkflowResult:
    """Result of a workflow execution."""
    workflow_id: str
    ticket_id: int
    stage: WorkflowStage
    category: str
    priority: str
    summary: str
    confidence: float
    draft: str
    requires_human_review: bool
    requires_approval: bool
    review_reason: str
    evidence: list[dict[str, Any]]
    audit_log: list[dict[str, Any]]
    error: str | None = None

class TicketProcessingGraph:
    """Four-stage pipeline for processing support tickets.

    Implements the control plane that orchestrates:
    1. Ticket classification
    2. Context retrieval (RAG)
    3. Response drafting
    4. Confidence checking
    5. Routing (auto-send / human review / approval)
    """

    def __init__(self):
        self._agent = get_agent()
        self._knowledge_base = get_knowledge_base()

    def process_ticket(self, ticket_id: int, subject: str, description: str, thread: str = "") -> WorkflowResult:
        """Process a ticket through the complete workflow."""
        state = TicketState(ticket_id=ticket_id, subject=subject, description=description, thread=thread)

        try:
            state = self._classify_ticket(state)
            state = self._retrieve_context(state)
            state = self._draft_answer(state)
            state = self._confidence_check(state)
        except ai_service.AIProviderError as exc:
            state.error = str(exc)
            state.log_action("workflow_error", {"error": str(exc)})
            logger.warning("Workflow failed for ticket %d: %s", ticket_id, exc)
        except Exception as exc:
            state.error = str(exc)
            state.log_action("workflow_error", {"error": str(exc)})
            logger.exception("Unexpected workflow error for ticket %d", ticket_id)

        return self._build_result(state)

    def _classify_ticket(self, state: TicketState) -> TicketState:
        """Classify the ticket using the AI provider."""
        state.stage = WorkflowStage.CLASSIFY
        state.log_action("classify_start")
        try:
            result = ai_service.analyze_ticket(state.subject, state.description)
            state.category = result.category.value
            state.priority = result.priority.value
            state.summary = result.summary
            state.confidence = result.confidence
            state.log_action("classify_complete", {"category": state.category, "priority": state.priority, "confidence": state.confidence})
        except ai_service.AIProviderError as exc:
            state.log_action("classify_failed", {"error": str(exc)})
            raise
        return state

    def _retrieve_context(self, state: TicketState) -> TicketState:
        """Retrieve relevant context from the knowledge base."""
        state.stage = WorkflowStage.RETRIEVE
        state.log_action("retrieve_start")
        try:
            evidence = self._knowledge_base.retrieve(f"{state.subject} {state.description}", top_k=3)
            state.evidence = [{"content": ev.content, "source": ev.source, "score": ev.score, "metadata": ev.metadata} for ev in evidence]
            state.log_action("retrieve_complete", {"evidence_count": len(state.evidence)})
        except Exception as exc:
            logger.warning("Context retrieval failed: %s", exc)
            state.log_action("retrieve_failed", {"error": str(exc)})
        return state

    def _draft_answer(self, state: TicketState) -> TicketState:
        """Draft a response using the AI provider and retrieved evidence."""
        state.stage = WorkflowStage.DRAFT
        state.log_action("draft_start")
        try:
            draft = self._agent.draft_response(subject=state.subject, description=state.description, thread=state.thread, evidence=state.evidence)
            try:
                guardrails.assert_safe_draft(draft, state.thread)
                state.draft = draft
                state.log_action("draft_complete", {"draft_length": len(draft)})
            except guardrails.GuardrailError as exc:
                if settings.ai_guardrail_mode == "fallback":
                    state.draft = guardrails.SAFE_FALLBACK_DRAFT
                    state.log_action("draft_guardrail_fallback", {"reason": str(exc)})
                else:
                    state.log_action("draft_guardrail_rejected", {"reason": str(exc)})
                    raise ai_service.AIProviderError(str(exc))
        except ai_service.AIProviderError as exc:
            state.log_action("draft_failed", {"error": str(exc)})
            raise
        return state

    def _confidence_check(self, state: TicketState) -> TicketState:
        """Check confidence and determine routing."""
        state.stage = WorkflowStage.CONFIDENCE_CHECK
        state.log_action("confidence_check_start", {"confidence": state.confidence})
        if state.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            state.confidence_level = ConfidenceLevel.HIGH
        elif state.confidence >= LOW_CONFIDENCE_THRESHOLD:
            state.confidence_level = ConfidenceLevel.MEDIUM
        else:
            state.confidence_level = ConfidenceLevel.LOW
        dangerous_patterns = ["refund", "cancel", "delete", "compensate", "guarantee"]
        draft_lower = state.draft.lower()
        has_dangerous_action = any(
            re.search(rf"\b{re.escape(pattern)}\b", draft_lower) for pattern in dangerous_patterns
        )
        if has_dangerous_action:
            state.requires_approval = True
            state.review_reason = "Draft contains potentially dangerous action"
            state.stage = WorkflowStage.APPROVAL_REQUIRED
        elif state.confidence_level == ConfidenceLevel.LOW:
            state.requires_human_review = True
            state.review_reason = f"Low confidence ({state.confidence:.2f})"
            state.stage = WorkflowStage.HUMAN_REVIEW
        elif state.confidence_level == ConfidenceLevel.MEDIUM:
            state.requires_human_review = True
            state.review_reason = f"Medium confidence ({state.confidence:.2f})"
            state.stage = WorkflowStage.HUMAN_REVIEW
        else:
            state.requires_human_review = False
            state.stage = WorkflowStage.AUTO_SEND
        state.log_action("confidence_check_complete", {"confidence_level": state.confidence_level.value, "requires_human_review": state.requires_human_review, "requires_approval": state.requires_approval, "review_reason": state.review_reason})
        return state

    def _build_result(self, state: TicketState) -> WorkflowResult:
        """Build the workflow result from the final state."""
        if state.error:
            state.stage = WorkflowStage.END
        return WorkflowResult(
            workflow_id=state.workflow_id, ticket_id=state.ticket_id, stage=state.stage,
            category=state.category, priority=state.priority, summary=state.summary,
            confidence=state.confidence, draft=state.draft,
            requires_human_review=state.requires_human_review, requires_approval=state.requires_approval,
            review_reason=state.review_reason, evidence=state.evidence,
            audit_log=state.audit_log, error=state.error,
        )


_graph: TicketProcessingGraph | None = None


def get_graph() -> TicketProcessingGraph:
    """Get or create the singleton TicketProcessingGraph instance."""
    global _graph
    if _graph is None:
        _graph = TicketProcessingGraph()
    return _graph


def reset_graph() -> None:
    """Reset the singleton (for testing)."""
    global _graph
    _graph = None
