"""Tests for TicketProcessingWorkflow running on a real Temporal test environment."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.workflows.definitions import (
    TicketProcessingWorkflow,
    TicketWorkflowInput,
    activity_classify,
    activity_draft,
    activity_retrieve,
)


def test_temporal_workflow_straight_through(monkeypatch):
    """Workflow runs end-to-end on Temporal test server without human review."""
    monkeypatch.setenv("AI_PROVIDER", "stub")

    async def _run():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            with ThreadPoolExecutor(max_workers=5) as pool:
                async with Worker(
                    env.client,
                    task_queue="test-tickets",
                    workflows=[TicketProcessingWorkflow],
                    activities=[activity_classify, activity_retrieve, activity_draft],
                    activity_executor=pool,
                ):
                    handle = await env.client.start_workflow(
                        TicketProcessingWorkflow.run,
                        TicketWorkflowInput(
                            ticket_id=101,
                            subject="How to change my password",
                            description="I cannot find the password change settings.",
                        ),
                        id="temporal-ticket-101",
                        task_queue="test-tickets",
                    )
                    result = await handle.result()
                    assert result.category == "authentication"
                    assert result.requires_approval is False
                    assert result.approved is False
                    assert len(result.draft) > 0

    asyncio.run(_run())


def test_temporal_workflow_suspends_and_resumes_on_approval(monkeypatch):
    """Workflow suspends for high-impact draft and resumes on signal_approve."""
    monkeypatch.setenv("AI_PROVIDER", "stub")

    async def _run():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            with ThreadPoolExecutor(max_workers=5) as pool:
                async with Worker(
                    env.client,
                    task_queue="test-tickets",
                    workflows=[TicketProcessingWorkflow],
                    activities=[activity_classify, activity_retrieve, activity_draft],
                    activity_executor=pool,
                ):
                    handle = await env.client.start_workflow(
                        TicketProcessingWorkflow.run,
                        TicketWorkflowInput(
                            ticket_id=102,
                            subject="Refund request",
                            description="I demand a refund for my subscription right now.",
                        ),
                        id="temporal-ticket-102",
                        task_queue="test-tickets",
                    )
                    # Signal approval to the suspended workflow
                    await handle.signal(TicketProcessingWorkflow.approve, args=["agent-lead", "Approved exception"])
                    result = await handle.result()
                    assert result.requires_approval is True
                    assert result.approved is True
                    assert result.sent is True

    asyncio.run(_run())
