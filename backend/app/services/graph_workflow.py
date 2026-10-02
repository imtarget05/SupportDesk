"""Ticket processing pipeline for AI-assisted support, built on LangGraph.

This is a real ``langgraph`` ``StateGraph`` with a ``MemorySaver`` checkpointer,
not a straight-line sequence dressed up as one. Each processing stage is a graph
node over a shared state, and the human-approval gate is a genuine
``interrupt()`` / ``Command(resume=...)`` round trip.

Nodes
    classify_ticket -> retrieve_context -> draft_answer -> confidence_check
                                                                       |
                                                        --------------|--------------
                                                        V                              V
                                              human_review / approval_required     end

Human-in-the-loop
    ``process_ticket(..., require_approval=True)`` pauses the graph at the
    approval node and returns the pending state. The agent then calls
    ``resume_ticket(...)`` to continue. The default (``require_approval=False``)
    runs straight through and only *reports* that approval is required, which is
    what the synchronous REST endpoint needs.

Stage failure is contained: a provider error is recorded in ``state.error`` and
the run finishes at the error state rather than propagating, so one bad LLM call
never takes down the ticket workflow.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, TypeVar, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import settings
from app.services import ai_service, guardrails
from app.services.langchain_agent import get_agent
from app.services.knowledge_base import get_knowledge_base

logger = logging.getLogger(__name__)

_E = TypeVar("_E", bound=Enum)


def _coerce(raw: str | None, enum_cls: type[_E], default: _E) -> _E:
    """Parse a graph-state string back into an enum, falling back to ``default``.

    Graph state is serialized as plain strings, so an unexpected value must
    degrade to a safe default rather than raise and break the run.
    """
    try:
        return enum_cls(raw)
    except (ValueError, TypeError):
        return default


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


def _append(old: list[Any] | None, new: list[Any] | None) -> list[Any]:
    """Reducer: concatenate list-returning node updates onto shared state.

    LangGraph merges node return values into the state instead of mutating it,
    so append-style fields (audit log, evidence) need an explicit reducer to
    accumulate rather than overwrite.
    """
    return list(old or []) + list(new or [])


class WorkflowState(TypedDict):
    """Graph state passed between nodes.

    Mirrors :class:`TicketState` (which stays the external contract for the API
    layer) but as a ``TypedDict`` with reducers, which is what ``StateGraph``
    requires.
    """

    ticket_id: int
    subject: str
    description: str
    thread: str
    category: str
    priority: str
    summary: str
    confidence: float
    evidence: Annotated[list[dict[str, Any]], _append]
    draft: str
    confidence_level: str
    stage: str
    requires_human_review: bool
    requires_approval: bool
    review_reason: str
    audit_log: Annotated[list[dict[str, Any]], _append]
    error: str | None
    workflow_id: str
    created_at: float
    approved: bool



class TicketProcessingGraph:
    """Four-stage LangGraph pipeline for processing support tickets.

    Compiles a ``StateGraph`` with a ``MemorySaver`` checkpointer so a run can
    pause at the approval gate and be resumed later by thread id.

    The ``_*`` stage methods remain public and operate on ``TicketState``
    directly, so each stage stays unit-testable in isolation; the graph nodes
    are thin wrappers mapping node dict state to and from those methods.
    """

    # Whether the current run pauses at the approval gate. Set per-run in
    # ``process_ticket``; the synchronous REST endpoint leaves it off because it
    # cannot answer mid-run.
    _approval_armed: bool = False

    def __init__(self) -> None:
        self._agent = get_agent()
        self._knowledge_base = get_knowledge_base()
        self._checkpointer = MemorySaver()
        self._app = self._compile()

    # -- state conversion ------------------------------------------------

    @staticmethod
    def _to_graph_state(state: TicketState) -> WorkflowState:
        return WorkflowState(
            ticket_id=state.ticket_id,
            subject=state.subject,
            description=state.description,
            thread=state.thread,
            category=state.category,
            priority=state.priority,
            summary=state.summary,
            confidence=state.confidence,
            evidence=list(state.evidence),
            draft=state.draft,
            confidence_level=state.confidence_level.value,
            stage=state.stage.value,
            requires_human_review=state.requires_human_review,
            requires_approval=state.requires_approval,
            review_reason=state.review_reason,
            audit_log=list(state.audit_log),
            error=state.error,
            workflow_id=state.workflow_id,
            created_at=state.created_at,
            approved=False,
        )

    @staticmethod
    def _to_ticket_state(payload: dict[str, Any]) -> TicketState:
        """Rebuild the external state object from a graph result."""
        state = TicketState(
            ticket_id=payload.get("ticket_id", 0),
            subject=payload.get("subject", ""),
            description=payload.get("description", ""),
        )
        state.thread = payload.get("thread", "")
        state.category = payload.get("category", "unknown")
        state.priority = payload.get("priority", "normal")
        state.summary = payload.get("summary", "")
        state.confidence = payload.get("confidence", 0.0)
        state.evidence = list(payload.get("evidence") or [])
        state.draft = payload.get("draft", "")
        state.confidence_level = _coerce(
            payload.get("confidence_level"), ConfidenceLevel, ConfidenceLevel.LOW
        )
        state.stage = _coerce(payload.get("stage"), WorkflowStage, WorkflowStage.END)
        state.requires_human_review = bool(payload.get("requires_human_review"))
        state.requires_approval = bool(payload.get("requires_approval"))
        state.review_reason = payload.get("review_reason", "")
        state.audit_log = list(payload.get("audit_log") or [])
        state.error = payload.get("error")
        state.workflow_id = payload.get("workflow_id", state.workflow_id)
        state.created_at = payload.get("created_at", state.created_at)
        return state

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

    # -- graph nodes ------------------------------------------------------

    def _run_stage(self, payload: WorkflowState, method_name: str) -> dict[str, Any]:
        """Run one stage, returning only what changed for the graph reducers.

        Nodes return deltas rather than the whole state: returning full lists
        would double them, since the list fields are append-reduced.
        """
        state = self._to_ticket_state(payload)
        before = len(state.audit_log)
        getattr(self, method_name)(state)
        return {
            "category": state.category,
            "priority": state.priority,
            "summary": state.summary,
            "confidence": state.confidence,
            "draft": state.draft,
            "evidence": state.evidence if method_name == "_retrieve_context" else [],
            "confidence_level": state.confidence_level.value,
            "stage": state.stage.value,
            "requires_human_review": state.requires_human_review,
            "requires_approval": state.requires_approval,
            "review_reason": state.review_reason,
            "error": state.error,
            "audit_log": state.audit_log[before:],
        }

    def _node_classify(self, payload: WorkflowState) -> dict[str, Any]:
        try:
            return self._run_stage(payload, "_classify_ticket")
        except ai_service.AIProviderError as exc:
            return {"error": str(exc), "stage": WorkflowStage.END.value}

    def _node_retrieve(self, payload: WorkflowState) -> dict[str, Any]:
        return self._run_stage(payload, "_retrieve_context")

    def _node_draft(self, payload: WorkflowState) -> dict[str, Any]:
        try:
            return self._run_stage(payload, "_draft_answer")
        except ai_service.AIProviderError as exc:
            return {"error": str(exc), "stage": WorkflowStage.END.value}

    def _node_confidence_check(self, payload: WorkflowState) -> dict[str, Any]:
        return self._run_stage(payload, "_confidence_check")

    def _node_approval_gate(self, payload: WorkflowState) -> dict[str, Any]:
        """Suspend for a human decision when the gate is armed.

        Uses LangGraph ``interrupt()``: the run stops, the state is
        checkpointed, and :meth:`resume_ticket` continues it. Only armed when
        the caller asked for it, since the synchronous REST endpoint cannot
        answer mid-run.
        """
        if not payload.get("requires_approval") or not self._approval_armed:
            return {"stage": WorkflowStage.END.value}
        decision = interrupt(
            {
                "reason": payload.get("review_reason", ""),
                "draft": payload.get("draft", ""),
                "ticket_id": payload.get("ticket_id"),
            }
        )
        approved = bool(isinstance(decision, dict) and decision.get("approved"))
        return {
            "approved": approved,
            "stage": WorkflowStage.SEND.value if approved else WorkflowStage.END.value,
            "requires_approval": not approved,
        }

    # -- graph assembly ---------------------------------------------------

    def _compile(self):
        builder = StateGraph(WorkflowState)
        builder.add_node("classify_ticket", self._node_classify)
        builder.add_node("retrieve_context", self._node_retrieve)
        builder.add_node("draft_answer", self._node_draft)
        builder.add_node("confidence_check", self._node_confidence_check)
        builder.add_node("approval_gate", self._node_approval_gate)

        builder.add_edge(START, "classify_ticket")
        builder.add_edge("classify_ticket", "retrieve_context")
        builder.add_edge("retrieve_context", "draft_answer")
        builder.add_edge("draft_answer", "confidence_check")
        builder.add_edge("confidence_check", "approval_gate")
        builder.add_edge("approval_gate", END)

        return builder.compile(checkpointer=self._checkpointer)

    # -- public entry points ----------------------------------------------

    def process_ticket(
        self,
        ticket_id: int,
        subject: str,
        description: str,
        thread: str = "",
        require_approval: bool = False,
    ) -> WorkflowResult:
        """Run a ticket through the compiled LangGraph pipeline.

        With ``require_approval=True`` the run suspends at the approval gate and
        the caller continues it with :meth:`resume_ticket`.
        """
        seed = TicketState(
            ticket_id=ticket_id, subject=subject, description=description, thread=thread
        )
        self._approval_armed = require_approval
        try:
            payload = self._app.invoke(
                self._to_graph_state(seed), self._config(seed.workflow_id)
            )
        finally:
            self._approval_armed = False
        return self._build_result(self._to_ticket_state(payload))

    def resume_ticket(self, workflow_id: str, approved: bool) -> WorkflowResult:
        """Continue a run that suspended at the approval gate.

        The gate is re-armed for the resumed run: on resume LangGraph re-executes
        the interrupting node, so a disarmed gate would return END immediately
        and discard the decision being delivered.
        """
        self._approval_armed = True
        try:
            payload = self._app.invoke(
                Command(resume={"approved": approved}), self._config(workflow_id)
            )
        finally:
            self._approval_armed = False
        return self._build_result(self._to_ticket_state(payload))

    def is_suspended(self, workflow_id: str) -> bool:
        """Whether a run is parked waiting for an approval decision."""
        snapshot = self._app.get_state(self._config(workflow_id))
        return snapshot.next == ("approval_gate",)

    @staticmethod
    def _config(workflow_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": workflow_id}}

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
