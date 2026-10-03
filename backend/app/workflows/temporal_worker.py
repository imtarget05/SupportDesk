"""Temporal client and worker entry points.

Kept separate from `definitions.py` so importing the workflow does not open a
connection. Nothing here runs on import: `python -m app.workflows.temporal_worker`
starts a worker, and `TemporalWorkflowClient` connects lazily on first use.

`TEMPORAL_ENABLED` is false by default, so this module is inert unless a
Temporal server is configured.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.workflows.definitions import (
    TicketProcessingWorkflow,
    TicketWorkflowInput,
    TicketWorkflowResult,
)
from app.workflows.pipeline import Pipeline

logger = logging.getLogger(__name__)

WORKFLOW_NAME = "TicketProcessingWorkflow"


def _reuse_policy():
    from temporalio.common import WorkflowIDReusePolicy

    return WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY


async def connect_client():
    """Connect to the configured Temporal server."""
    from temporalio.client import Client

    return await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )


@dataclass
class TemporalWorkflowClient:
    """Start and signal ticket workflows on a Temporal server."""

    async def start(
        self, request: TicketWorkflowInput, client: Any | None = None
    ) -> str:
        """Start a workflow and return its workflow id.

        The id defaults to the request id, and reuse is set to
        ``ALLOW_DUPLICATE_FAILED_ONLY`` so a retry after a *failed* run can
        start again, while a double-submit of a still-running workflow is
        rejected rather than drafting the reply twice.
        """
        owned = client is None
        client = client or await connect_client()
        try:
            handle = await client.start_workflow(
                TicketProcessingWorkflow.run,
                request,
                id=request.request_id or f"ticket-{request.ticket_id}",
                task_queue=settings.temporal_task_queue,
                id_reuse_policy=_reuse_policy(),
            )
            return handle.id
        finally:
            if owned:
                await client.close()

    async def signal(
        self,
        workflow_id: str,
        approved: bool,
        decided_by: str = "agent",
        reason: str = "",
        client: Any | None = None,
    ) -> None:
        """Deliver an approval decision to a suspended workflow."""
        owned = client is None
        client = client or await connect_client()
        try:
            handle = client.get_workflow_handle(workflow_id)
            signal = handle.signal_approve if approved else handle.signal_reject
            await signal(decided_by, reason)
        finally:
            if owned:
                await client.close()

    async def result(
        self, workflow_id: str, client: Any | None = None
    ) -> TicketWorkflowResult:
        """Wait for and return a workflow's result."""
        owned = client is None
        client = client or await connect_client()
        try:
            handle = client.get_workflow_handle(workflow_id)
            return await handle.result()
        finally:
            if owned:
                await client.close()


async def run_worker() -> None:
    """Run a Temporal worker serving the ticket workflow until interrupted."""
    from temporalio.worker import Worker

    client = await connect_client()
    pipeline = Pipeline()
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[TicketProcessingWorkflow],
        activities=[pipeline.classify, pipeline.retrieve, pipeline.draft],
    )
    logger.info("Temporal worker started on task queue %s", settings.temporal_task_queue)
    try:
        await worker.run()
    finally:
        await client.close()


if __name__ == "__main__":  # pragma: no cover — process entry point
    asyncio.run(run_worker())