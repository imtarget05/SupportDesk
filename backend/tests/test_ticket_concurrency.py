"""Optimistic-concurrency guard on ticket status transitions.

A plain read-then-write of `Ticket.status` is last-write-win under PostgreSQL's
READ COMMITTED isolation: two agents patching the same ticket both read `open`,
both decide their transition is legal, and the second silently discards the
first. The service now writes through a conditional UPDATE guarded on the status
it validated against, so the loser of the race gets 0 rows affected and a 409.
"""

import dataclasses

import pytest
from sqlalchemy import update

from app.database import SessionLocal
from app.enums import TicketStatus
from app.models import Ticket
from app.services import ticket_service
from tests.conftest import create_ticket


@pytest.fixture()
def in_progress_ticket(client, agent_headers):
    ticket_id = create_ticket(client, email="race@example.com").json()["id"]
    res = client.patch(
        f"/api/tickets/{ticket_id}", json={"status": "in_progress"}, headers=agent_headers
    )
    assert res.status_code == 200, res.text
    return ticket_id


def test_concurrent_conflicting_transition_returns_409_and_does_not_overwrite(
    client, agent_headers, in_progress_ticket, monkeypatch
):
    """A transition that loses the race is rejected, not silently applied.

    The competing write is injected between this request's read of the status
    and its write, which is exactly the window READ COMMITTED leaves open.
    """
    original_validate = ticket_service.validate_transition

    def competing_write(current, target):
        # A second agent, on its own connection/transaction, wins the race:
        # in_progress -> waiting lands before our guarded UPDATE runs.
        other = SessionLocal()
        try:
            other.execute(
                update(Ticket)
                .where(Ticket.id == in_progress_ticket, Ticket.status == current.value)
                .values(status=TicketStatus.WAITING.value)
            )
            other.commit()
        finally:
            other.close()
        return original_validate(current, target)

    monkeypatch.setattr(ticket_service, "validate_transition", competing_write)

    # Our request still believes the ticket is `in_progress` and wants to
    # resolve it. `in_progress -> resolved` is a legal edge, so only the guard
    # can reject this.
    res = client.patch(
        f"/api/tickets/{in_progress_ticket}",
        json={"status": "resolved"},
        headers=agent_headers,
    )
    assert res.status_code == 409, res.text

    detail = res.json()["detail"]
    assert "waiting" in detail and "resolved" in detail

    # The winning write survives; we did not clobber it.
    current = client.get(f"/api/tickets/{in_progress_ticket}", headers=agent_headers).json()
    assert current["status"] == "waiting"


def test_guarded_update_still_succeeds_when_nobody_competes(
    client, agent_headers, in_progress_ticket, monkeypatch
):
    """The guard must not reject a transition that actually won the race."""
    calls = {"n": 0}
    original_validate = ticket_service.validate_transition

    def counting_validate(current, target):
        calls["n"] += 1
        return original_validate(current, target)

    monkeypatch.setattr(ticket_service, "validate_transition", counting_validate)

    res = client.patch(
        f"/api/tickets/{in_progress_ticket}",
        json={"status": "resolved"},
        headers=agent_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "resolved"
    assert calls["n"] == 1


def test_second_patch_in_a_new_request_sees_committed_state(
    client, agent_headers, in_progress_ticket
):
    """Regression guard: a later request reads the committed status, not a
    stale cached one, so the guard does not fire spuriously."""
    assert client.patch(
        f"/api/tickets/{in_progress_ticket}", json={"status": "waiting"}, headers=agent_headers
    ).status_code == 200
    assert client.patch(
        f"/api/tickets/{in_progress_ticket}",
        json={"status": "in_progress"},
        headers=agent_headers,
    ).status_code == 200


def test_illegal_transition_still_reports_current_status(client, agent_headers, in_progress_ticket):
    """The pre-existing 409 path is unchanged."""
    res = client.patch(
        f"/api/tickets/{in_progress_ticket}", json={"status": "closed"}, headers=agent_headers
    )
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "in_progress" in detail and "closed" in detail