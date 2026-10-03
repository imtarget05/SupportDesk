"""Durable ticket-processing workflow definitions.

These are the Temporal definitions. The same stages also run in-process via
``local_runner.py``, so the workflow is exercisable without a Temporal server
while production still gets durability: retries across restarts, resumable
approval waits, and an auditable execution history.

Stages:
    classify -> retrieve -> draft -> await_approval -> send

The approval wait is the reason this is Temporal and not a plain function. A
human can take minutes or hours to decide; the process may restart in between.
With Temporal the workflow suspends on a signal and resumes with the decision
recorded in history, rather than holding a worker thread or losing the run.

``TEMPORAL_ENABLED`` gates only the *runtime*. The workflow bodies below are
plain definitions and stay importable either way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity, workflow

from app.config import settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.workflows.pipeline import Pipeline

logger = logging.getLogger(__name__)


def stage_timeout_seconds() -> int:
    """Activity timeout in seconds, read from settings at call time.

    Defined here (rather than imported from `pipeline`) because the Temporal
    workflow body runs inside the SDK sandbox, which restricts imports.
    """
    return int(getattr(settings, "workflow_stage_timeout_s", 120) or 120)

# Thresholds mirror graph_workflow: the workflow stops for approval when the
# draft is high-impact, regardless of how confident the classifier was.
DANGEROUS_TERMS = ("refund", "cancel", "delete", "compensate", "guarantee")

APPROVAL_TIMEOUT_MINUTES = 60


@dataclass
class TicketWorkflowInput:
    """Everything a run needs; must be serializable by Temporal."""

    ticket_id: int
    subject: str
    description: str
    thread: str = ""
    request_id: str = ""


@dataclass
class TicketWorkflowResult:
    """What the workflow produced."""

    ticket_id: int
    category: str
    priority: str
    summary: str
    confidence: float
    draft: str
    requires_approval: bool
    approved: bool
    sent: bool
    review_reason: str = ""
    error: str | None = None


@dataclass
class ApprovalState:
    """Mutable approval state, updated by the approval signal."""

    decided: bool = False
    approved: bool = False
    decided_by: str = ""
    reason: str = ""


@dataclass
class WorkflowStages:
    """Per-stage results, accumulated across the run."""

@dataclass
class WorkflowStages:
    """Per-stage results, accumulated across the run."""

    evidence: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


def needs_approval(draft: str, category: str) -> tuple[bool, str]:
    """Whether this draft must wait for a human, and why.

    Split out from the workflow body so both runtimes apply exactly the same
    rule; a local run and a Temporal run must never disagree about escalation.
    """
    lowered = (draft or "").lower()
    for term in DANGEROUS_TERMS:
        if term in lowered:
            return True, f"draft mentions a high-impact action ({term})"
    if category == "refund":
        return True, "refund category always requires approval"
    return False, ""


# --------------------------------------------------------------- activities


def activity_classify(payload: dict[str, Any]) -> dict[str, Any]:
    """Activity: triage the ticket through the configured AI provider."""
    from app.services import ai_service

    result = ai_service.analyze_ticket(
        payload["subject"], payload["description"], ticket_id=payload["ticket_id"]
    )
    return {
        "category": result.category.value,
        "priority": result.priority.value,
        "summary": result.summary,
        "confidence": result.confidence,
    }


def activity_retrieve(payload: dict[str, Any]) -> dict[str, Any]:
    """Activity: pull knowledge-base evidence for the ticket."""
    from app.services.knowledge_base import get_knowledge_base

    evidence = get_knowledge_base().retrieve(
        f"{payload['subject']} {payload['description']}", top_k=3
    )
    return {
        "evidence": [
            {"content": e.content, "source": e.source, "score": e.score} for e in evidence
        ]
    }


def activity_draft(payload: dict[str, Any]) -> dict[str, Any]:
    """Activity: draft a reply grounded in the retrieved evidence."""
    from app.services import ai_service

    thread = payload.get("thread", "")
    evidence = payload.get("evidence") or []
    if evidence:
        thread += "\n\nRelevant knowledge base entries:\n" + "\n".join(
            f"[{i}] {e.get('source', 'unknown')}: {e.get('content', '')}"
            for i, e in enumerate(evidence, 1)
        )
    draft = ai_service.suggest_response(
        payload["subject"], payload["description"], thread, ticket_id=payload["ticket_id"]
    )
    return {"draft": draft}


# ---------------------------------------------------- Temporal workflow

_PIPELINE: "Pipeline | None" = None


def set_pipeline(pipeline: "Pipeline") -> None:
    """Install the stage implementations the workflow will use.

    A module-level slot rather than a constructor argument because Temporal
    rejects local workflow classes: the workflow must be a module-level symbol
    for the SDK to sandbox it, and it cannot close over a per-instance value.
    """
    global _PIPELINE
    _PIPELINE = pipeline


def get_pipeline() -> "Pipeline":
    """The installed pipeline; the default one when none was set."""
    global _PIPELINE
    if _PIPELINE is None:
        from app.workflows.pipeline import Pipeline

        _PIPELINE = Pipeline()
    return _PIPELINE


@workflow.defn(name="TicketProcessingWorkflow")
class TicketProcessingWorkflow:
    """Durable ticket pipeline: classify -> retrieve -> draft -> approval -> send."""

    def __init__(self) -> None:
        self._approval = ApprovalState()

    @workflow.run
    async def run(self, request: TicketWorkflowInput) -> TicketWorkflowResult:
        pipeline = get_pipeline()
        timeout = timedelta(seconds=stage_timeout_seconds())
        payload: dict[str, Any] = {
            "ticket_id": request.ticket_id,
            "subject": request.subject,
            "description": request.description,
            "thread": request.thread,
        }

        payload.update(
            await workflow.execute_activity(
                pipeline.classify, payload, start_to_close_timeout=timeout
            )
        )
        retrieval = await workflow.execute_activity(
            pipeline.retrieve, payload, start_to_close_timeout=timeout
        )
        payload["evidence"] = retrieval.get("evidence", [])
        drafted = await workflow.execute_activity(
            pipeline.draft, payload, start_to_close_timeout=timeout
        )
        payload["draft"] = drafted["draft"]

        requires_approval, reason = needs_approval(
            payload["draft"], payload.get("category", "")
        )

        approved = False
        if requires_approval:
            # Suspends here holding no worker; resumes on the signal. This is
            # the part that needs durability: the process may restart while it
            # waits, and the decision must survive that.
            await workflow.wait_condition(lambda: self._approval.decided)
            approved = self._approval.approved

        return TicketWorkflowResult(
            ticket_id=request.ticket_id,
            category=payload.get("category", "unknown"),
            priority=payload.get("priority", "normal"),
            summary=payload.get("summary", ""),
            confidence=payload.get("confidence", 0.0),
            draft=payload["draft"],
            requires_approval=requires_approval,
            approved=approved,
            sent=approved,
            review_reason=reason,
        )

    @workflow.signal
    async def approve(self, decided_by: str, reason: str = "") -> None:
        self._approval.decided = True
        self._approval.approved = True
        self._approval.decided_by = decided_by
        self._approval.reason = reason

    @workflow.signal
    async def reject(self, decided_by: str, reason: str = "") -> None:
        self._approval.decided = True
        self._approval.approved = False
        self._approval.decided_by = decided_by
        self._approval.reason = reason

    @workflow.query
    def approval_state(self) -> ApprovalState:
        return self._approval
