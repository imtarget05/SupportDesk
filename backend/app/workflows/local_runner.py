"""In-process runner for the ticket workflow.

Executes exactly the stages the Temporal workflow executes, in the same order,
with the same retry ceiling and the same approval rule. Used when
``TEMPORAL_ENABLED=false``, which is the default: development and the test
suite then need no Temporal server.

What this runner does *not* give you is durability. It holds no history, it
cannot survive a restart mid-run, and it waits in-process for an approval. That
is the honest trade: the workflow logic is exercised and tested for free, while
production sets ``TEMPORAL_ENABLED=true`` and gets the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.workflows.definitions import (
    ApprovalState,
    TicketWorkflowInput,
    TicketWorkflowResult,
    needs_approval,
)
from app.workflows.pipeline import Pipeline, run_stage

logger = logging.getLogger(__name__)


@dataclass
class LocalRun:
    """A run in progress, resumable by workflow id."""

    workflow_id: str
    result: TicketWorkflowResult
    approval: ApprovalState
    evidence: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    finished: bool = False


class LocalWorkflowRunner:
    """Run the workflow stages in-process, with a resumable approval gate."""

    def __init__(self, pipeline: Pipeline | None = None) -> None:
        self._pipeline = pipeline or Pipeline()
        self._runs: dict[str, LocalRun] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"local-{self._counter}"

    def start(self, request: TicketWorkflowInput) -> LocalRun:
        """Run the pipeline, stopping before `send` when approval is needed."""
        workflow_id = request.request_id or self._next_id()
        payload: dict[str, Any] = {
            "ticket_id": request.ticket_id,
            "subject": request.subject,
            "description": request.description,
            "thread": request.thread,
        }
        events: list[str] = []
        try:
            payload.update(run_stage(self._pipeline.classify, payload))
            events.append("classified")

            retrieval = run_stage(self._pipeline.retrieve, payload)
            payload["evidence"] = retrieval.get("evidence", [])
            events.append("retrieved")

            payload["draft"] = run_stage(self._pipeline.draft, payload)["draft"]
            events.append("drafted")
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            logger.warning("Local workflow %s failed: %s", workflow_id, exc)
            run = LocalRun(
                workflow_id=workflow_id,
                result=TicketWorkflowResult(
                    ticket_id=request.ticket_id,
                    category=payload.get("category", "unknown"),
                    priority=payload.get("priority", "normal"),
                    summary=payload.get("summary", ""),
                    confidence=payload.get("confidence", 0.0),
                    draft="",
                    requires_approval=False,
                    approved=False,
                    sent=False,
                    error=str(exc),
                ),
                approval=ApprovalState(),
                evidence=payload.get("evidence", []),
                events=events + ["error"],
                finished=True,
            )
            self._runs[workflow_id] = run
            return run

        requires_approval, reason = needs_approval(
            payload["draft"], payload.get("category", "")
        )
        events.append(f"approval_required:{requires_approval}")

        approval = ApprovalState()
        approved = not requires_approval
        if not requires_approval:
            events.append("auto_approved")

        run = LocalRun(
            workflow_id=workflow_id,
            result=TicketWorkflowResult(
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
            ),
            approval=approval,
            evidence=payload.get("evidence", []),
            events=events,
            finished=not requires_approval,
        )
        self._runs[workflow_id] = run
        return run

    def decide(
        self, workflow_id: str, approved: bool, decided_by: str = "agent", reason: str = ""
    ) -> LocalRun:
        """Deliver an approval decision, mirroring the Temporal signals."""
        run = self._runs.get(workflow_id)
        if run is None:
            raise KeyError(f"unknown workflow run: {workflow_id}")
        run.approval.decided = True
        run.approval.approved = approved
        run.approval.decided_by = decided_by
        run.approval.reason = reason
        run.result.approved = approved
        run.result.sent = approved
        run.events.append(f"approval_{'granted' if approved else 'rejected'}")
        run.finished = True
        return run

    def get(self, workflow_id: str) -> LocalRun | None:
        return self._runs.get(workflow_id)

    def pending(self) -> list[str]:
        """Runs waiting for an approval decision."""
        return [wid for wid, run in self._runs.items() if not run.finished]


_runner: LocalWorkflowRunner | None = None


def get_runner() -> LocalWorkflowRunner:
    global _runner
    if _runner is None:
        _runner = LocalWorkflowRunner()
    return _runner


def reset_runner() -> None:
    """Reset the singleton (for testing)."""
    global _runner
    _runner = None