"""Durable ticket workflow: stage retries, approval gate, and the local runner.

The local runner executes the same stage functions, in the same order, with the
same retry ceiling and the same approval rule as the Temporal workflow — so
these tests cover the workflow logic without needing a Temporal server.
"""

import pytest

from app.workflows import definitions as defs
from app.workflows.definitions import (
    TicketProcessingWorkflow,
    TicketWorkflowInput,
    needs_approval,
)
from app.workflows.local_runner import LocalWorkflowRunner, reset_runner
from app.workflows.pipeline import (
    MAX_STAGE_ATTEMPTS,
    Pipeline,
    StageError,
    run_stage,
)
from tests.conftest import create_ticket


@pytest.fixture(autouse=True)
def _reset():
    yield
    reset_runner()


def _input(**overrides) -> TicketWorkflowInput:
    payload = {
        "ticket_id": 42,
        "subject": "Cannot log in",
        "description": "The password reset link never arrives in my inbox.",
    }
    payload.update(overrides)
    return TicketWorkflowInput(**payload)


def _pipeline(draft: str = "Thanks for reaching out. Could you confirm the details?") -> Pipeline:
    return Pipeline(
        classify=lambda p: {
            "category": "authentication",
            "priority": "normal",
            "summary": "Login problem",
            "confidence": 0.8,
        },
        retrieve=lambda p: {"evidence": [{"content": "reset link", "source": "faq.md"}]},
        draft=lambda p: {"draft": draft},
    )


# ------------------------------------------------------------ approval rule


@pytest.mark.parametrize(
    "draft",
    ["We will refund you today", "I can cancel that subscription", "Full guarantee provided"],
)
def test_high_impact_drafts_require_approval(draft):
    required, reason = needs_approval(draft, "other")
    assert required is True
    assert reason


def test_safe_draft_needs_no_approval():
    required, reason = needs_approval(
        "Could you confirm the order number so I can check?", "other"
    )
    assert required is False
    assert reason == ""


def test_refund_category_always_requires_approval():
    """A refund ticket is high-impact even when the draft text is bland."""
    required, reason = needs_approval("Following the steps above.", "refund")
    assert required is True
    assert "refund category" in reason


# ------------------------------------------------------------- stage retries


def test_successful_stage_returns_its_result():
    assert run_stage(lambda p: {"ok": True}, {"x": 1}) == {"ok": True}


def test_transient_failure_is_retried():
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"ok": True}

    assert run_stage(flaky, {}) == {"ok": True}
    assert calls["n"] == 3


def test_permanent_failure_raises_after_the_ceiling():
    calls = {"n": 0}

    def always_fails(payload):
        calls["n"] += 1
        raise RuntimeError("permanent")

    with pytest.raises(StageError, match="failed after"):
        run_stage(always_fails, {})
    assert calls["n"] == MAX_STAGE_ATTEMPTS


def test_failure_error_names_the_stage():
    def activity_classify(payload):
        raise RuntimeError("boom")

    with pytest.raises(StageError, match="activity_classify"):
        run_stage(activity_classify, {})


# --------------------------------------------------------------- local runner


def test_run_produces_a_result():
    run = LocalWorkflowRunner(_pipeline()).start(_input())
    assert run.result.ticket_id == 42
    assert run.result.category == "authentication"
    assert run.result.draft
    assert run.finished is True


def test_run_records_its_events():
    run = LocalWorkflowRunner(_pipeline()).start(_input())
    assert run.events[:3] == ["classified", "retrieved", "drafted"]


def test_run_carries_evidence_forward():
    run = LocalWorkflowRunner(_pipeline()).start(_input())
    assert run.evidence[0]["source"] == "faq.md"


def test_high_impact_draft_suspends_for_approval():
    run = LocalWorkflowRunner(_pipeline(draft="We will issue a refund now.")).start(_input())
    assert run.result.requires_approval is True
    assert run.result.sent is False
    assert run.finished is False


def test_pending_lists_suspended_runs():
    runner = LocalWorkflowRunner(_pipeline(draft="We will issue a refund now."))
    run = runner.start(_input())
    assert runner.pending() == [run.workflow_id]


def test_approval_resumes_the_run():
    runner = LocalWorkflowRunner(_pipeline(draft="We will issue a refund now."))
    run = runner.start(_input())
    decided = runner.decide(run.workflow_id, approved=True, decided_by="agent-7")
    assert decided.result.sent is True
    assert decided.result.approved is True
    assert decided.approval.decided_by == "agent-7"
    assert decided.finished is True


def test_rejection_prevents_sending():
    runner = LocalWorkflowRunner(_pipeline(draft="We will issue a refund now."))
    run = runner.start(_input())
    decided = runner.decide(run.workflow_id, approved=False, reason="needs verification")
    assert decided.result.sent is False
    assert decided.approval.reason == "needs verification"


def test_run_ids_are_unique():
    runner = LocalWorkflowRunner(_pipeline())
    first = runner.start(_input()).workflow_id
    second = runner.start(_input()).workflow_id
    assert first != second


def test_explicit_request_id_is_used():
    run = LocalWorkflowRunner(_pipeline()).start(_input(request_id="req-abc"))
    assert run.workflow_id == "req-abc"


def test_unknown_run_cannot_be_decided():
    with pytest.raises(KeyError):
        LocalWorkflowRunner(_pipeline()).decide("does-not-exist", approved=True)


def test_stage_failure_is_reported_not_raised():
    def broken(payload):
        raise RuntimeError("provider down")

    run = LocalWorkflowRunner(Pipeline(classify=broken)).start(_input())
    assert run.result.error is not None
    assert run.result.sent is False
    assert "error" in run.events


def test_get_returns_a_stored_run():
    runner = LocalWorkflowRunner(_pipeline())
    run = runner.start(_input())
    assert runner.get(run.workflow_id) is run


# ----------------------------------------------------- Temporal definitions


def test_workflow_is_registered_with_temporal():
    """The workflow must be a real, module-level Temporal definition."""
    from temporalio.workflow import _Definition

    definition = TicketProcessingWorkflow.__temporal_workflow_definition
    assert isinstance(definition, _Definition)
    assert definition.name == "TicketProcessingWorkflow"


def test_workflow_exposes_approval_signals():
    definition = TicketProcessingWorkflow.__temporal_workflow_definition
    assert "approve" in definition.signals
    assert "reject" in definition.signals


def test_workflow_exposes_an_approval_query():
    definition = TicketProcessingWorkflow.__temporal_workflow_definition
    assert "approval_state" in definition.queries


def test_workflow_input_is_serializable():
    """Temporal ships arguments as JSON, so the input must be plain data."""
    import json
    from dataclasses import asdict

    payload = asdict(_input(request_id="r1"))
    assert json.loads(json.dumps(payload))["ticket_id"] == 42


def test_workflow_result_is_serializable():
    import json
    from dataclasses import asdict

    run = LocalWorkflowRunner(_pipeline()).start(_input())
    assert json.loads(json.dumps(asdict(run.result)))["ticket_id"] == 42
def test_reuse_policy_allows_a_retry_only_after_failure():
    """A double-submit of a live run must not draft the reply twice."""
    from temporalio.common import WorkflowIDReusePolicy

    from app.workflows.temporal_worker import _reuse_policy

    assert _reuse_policy() == WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY


def test_default_pipeline_uses_the_real_activities():
    pipeline = Pipeline()
    assert pipeline.classify is defs.activity_classify
    assert pipeline.retrieve is defs.activity_retrieve
    assert pipeline.draft is defs.activity_draft
def test_default_pipeline_uses_the_real_activities():
    pipeline = Pipeline()
    assert pipeline.classify is defs.activity_classify
    assert pipeline.retrieve is defs.activity_retrieve
    assert pipeline.draft is defs.activity_draft


# ---------------------------------------------------------------- API layer


def test_workflow_run_endpoint_requires_agent(client, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Login issue", description="Locked out."
    ).json()["id"]
    assert client.post(f"/api/tickets/{ticket_id}/ai/workflow/run").status_code == 401
    assert (
        client.post(
            f"/api/tickets/{ticket_id}/ai/workflow/run", headers=customer_headers
        ).status_code
        == 403
    )


def test_workflow_run_endpoint_returns_a_result(client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Cannot log in",
        description="The password reset link never arrives at all.",
    ).json()["id"]

    res = client.post(f"/api/tickets/{ticket_id}/ai/workflow/run", headers=agent_headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["workflow_id"]
    assert body["ticket_id"] == ticket_id
    assert body["draft"]
    assert body["events"][:3] == ["classified", "retrieved", "drafted"]
    assert body["error"] is None


def test_workflow_run_on_missing_ticket_is_404(client, agent_headers):
    assert client.post("/api/tickets/999999/ai/workflow/run", headers=agent_headers).status_code == 404


def test_workflow_run_get_returns_current_state(client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Refund question",
        description="I would like a refund for the duplicate charge.",
    ).json()["id"]
    started = client.post(
        f"/api/tickets/{ticket_id}/ai/workflow/run", headers=agent_headers
    ).json()

    res = client.get(
        f"/api/tickets/{ticket_id}/ai/workflow/{started['workflow_id']}",
        headers=agent_headers,
    )
    assert res.status_code == 200
    assert res.json()["workflow_id"] == started["workflow_id"]


def test_workflow_decision_unknown_run_is_404(client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Refund question", description="Refund please."
    ).json()["id"]
    res = client.post(
        f"/api/tickets/{ticket_id}/ai/workflow/nope/decision?approved=true",
        headers=agent_headers,
    )
    assert res.status_code == 404


def test_workflow_decision_for_another_ticket_is_409(client, agent_headers, customer_headers):
    """A run id must not be usable against a different ticket."""
    first = create_ticket(
        client,
        headers=customer_headers,
        subject="Refund question",
        description="I would like a refund for the duplicate charge.",
    ).json()["id"]
    second = create_ticket(
        client,
        headers=customer_headers,
        subject="Cannot log in",
        description="The password reset link never arrives at all.",
    ).json()["id"]

    started = client.post(f"/api/tickets/{first}/ai/workflow/run", headers=agent_headers).json()
    res = client.post(
        f"/api/tickets/{second}/ai/workflow/{started['workflow_id']}/decision?approved=true",
        headers=agent_headers,
    )
    assert res.status_code == 409


def test_workflow_approval_resumes_the_run(client, agent_headers, customer_headers):
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Refund question",
        description="I would like a refund for the duplicate charge.",
    ).json()["id"]
    started = client.post(
        f"/api/tickets/{ticket_id}/ai/workflow/run", headers=agent_headers
    ).json()
    assert started["requires_approval"] is True

    decided = client.post(
        f"/api/tickets/{ticket_id}/ai/workflow/{started['workflow_id']}/decision"
        "?approved=true&decided_by=agent-1",
        headers=agent_headers,
    )
    assert decided.status_code == 200
    assert decided.json()["approved"] is True
    assert any("approval_granted" in e for e in decided.json()["events"])


def test_workflow_never_creates_a_message(
    client, agent_headers, customer_headers, db_session
):
    """The workflow must not send anything, even after approval."""
    from app.models import Message

    before = db_session.query(Message).count()
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Refund question",
        description="I would like a refund for the duplicate charge.",
    ).json()["id"]
    started = client.post(
        f"/api/tickets/{ticket_id}/ai/workflow/run", headers=agent_headers
    ).json()
    client.post(
        f"/api/tickets/{ticket_id}/ai/workflow/{started['workflow_id']}/decision"
        "?approved=true",
        headers=agent_headers,
    )
    assert db_session.query(Message).count() == before

