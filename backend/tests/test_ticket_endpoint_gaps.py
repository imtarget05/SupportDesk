from tests.conftest import create_ticket, make_user


def test_assign_ticket(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="Printer broken")
    ticket_id = res.json()["id"]

    assign_res = client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": 1, "assignee_name": "agent", "notes": "Taking this ticket"},
    )
    assert assign_res.status_code == 200
    assert assign_res.json()["id"] == ticket_id


def test_transition_ticket_valid(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="Network down")
    ticket_id = res.json()["id"]

    # OPEN -> IN_PROGRESS is valid
    trans_res = client.post(
        f"/api/tickets/{ticket_id}/transition",
        headers=agent_headers,
        json={"status": "in_progress"},
    )
    assert trans_res.status_code == 200
    assert trans_res.json()["status"] == "in_progress"


def test_transition_ticket_invalid_conflict(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="Email not working")
    ticket_id = res.json()["id"]

    # OPEN -> RESOLVED is invalid according to state machine (must be in_progress first)
    trans_res = client.post(
        f"/api/tickets/{ticket_id}/transition",
        headers=agent_headers,
        json={"status": "resolved"},
    )
    assert trans_res.status_code == 409


def test_get_ticket_audit(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="VPN issue")
    ticket_id = res.json()["id"]

    # Perform assign and transition to populate audit trail
    client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": 1, "notes": "Assigned to primary"},
    )
    client.post(
        f"/api/tickets/{ticket_id}/transition",
        headers=agent_headers,
        json={"status": "in_progress"},
    )

    audit_res = client.get(f"/api/tickets/{ticket_id}/audit", headers=agent_headers)
    assert audit_res.status_code == 200
    audit_data = audit_res.json()
    assert isinstance(audit_data, list)
    actions = [e["action"] for e in audit_data]
    assert "ticket.assign" in actions
    assert "ticket.transition" in actions


def test_automation_jobs(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="Password reset request")
    ticket_id = res.json()["id"]

    job_res = client.post(
        "/api/automation/jobs",
        headers=agent_headers,
        json={"ticket_id": ticket_id, "workflow_name": "ticket_workflow"},
    )
    assert job_res.status_code == 200
    body = job_res.json()
    assert body["ticket_id"] == ticket_id
    assert "workflow_id" in body


def test_assign_persists_assignee_and_returns_it(
    client, agent_headers, agent_user, db_session
):
    res = create_ticket(client, email="user@example.com", subject="Printer broken")
    ticket_id = res.json()["id"]
    assert res.json()["assignee"] is None

    assign_res = client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": agent_user.id, "notes": "Taking this ticket"},
    )
    assert assign_res.status_code == 200, assign_res.text
    assert assign_res.json()["assignee"]["id"] == agent_user.id
    assert assign_res.json()["assignee"]["email"] == agent_user.email

    # Persistence: a fresh read (new request + expired session state) still
    # carries the assignee — this is a DB write, not an audit-only log.
    db_session.expire_all()
    got = client.get(f"/api/tickets/{ticket_id}", headers=agent_headers)
    assert got.status_code == 200
    assert got.json()["assignee"]["id"] == agent_user.id


def test_reassignment_records_previous_assignee(
    client, agent_headers, agent_user, db_session
):
    from tests.conftest import make_user as _make

    second = _make(
        db_session,
        email="agent2@test.dev",
        name="Second Agent",
        role="agent",
        password="agentpw2",
    )
    res = create_ticket(client, email="user@example.com", subject="Reassign me")
    ticket_id = res.json()["id"]
    client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": agent_user.id},
    )
    re_res = client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": second.id},
    )
    assert re_res.status_code == 200
    assert re_res.json()["assignee"]["id"] == second.id

    audit_res = client.get(f"/api/tickets/{ticket_id}/audit", headers=agent_headers)
    assigns = [e for e in audit_res.json() if e["action"] == "ticket.assign"]
    assert len(assigns) == 2
    assert assigns[1]["details"]["previous_assignee_id"] == agent_user.id


def test_assign_invalid_assignee_is_404(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="Bad assignee")
    ticket_id = res.json()["id"]
    r = client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": 999999},
    )
    assert r.status_code == 404


def test_assign_customer_as_assignee_is_422(
    client, agent_headers, customer_user
):
    res = create_ticket(client, email="user@example.com", subject="Cust assignee")
    ticket_id = res.json()["id"]
    r = client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": customer_user.id},
    )
    assert r.status_code == 422


def test_assign_by_customer_is_forbidden(client, customer_headers):
    res = create_ticket(
        client, subject="Customer assign attempt", headers=customer_headers
    )
    ticket_id = res.json()["id"]
    r = client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=customer_headers,
        json={"assignee_id": 1},
    )
    assert r.status_code == 403


def test_assign_missing_assignee_id_is_422(client, agent_headers):
    res = create_ticket(client, email="user@example.com", subject="No assignee id")
    ticket_id = res.json()["id"]
    r = client.post(
        f"/api/tickets/{ticket_id}/assign", headers=agent_headers, json={}
    )
    assert r.status_code == 422


def test_assign_unknown_ticket_is_404(client, agent_headers, agent_user):
    r = client.post(
        "/api/tickets/999999/assign",
        headers=agent_headers,
        json={"assignee_id": agent_user.id},
    )
    assert r.status_code == 404


def test_audit_survives_session_reload_and_orders_chronologically(
    client, agent_headers, agent_user, db_session
):
    res = create_ticket(client, email="user@example.com", subject="Audit durable")
    ticket_id = res.json()["id"]
    client.post(
        f"/api/tickets/{ticket_id}/assign",
        headers=agent_headers,
        json={"assignee_id": agent_user.id},
    )
    client.post(
        f"/api/tickets/{ticket_id}/transition",
        headers=agent_headers,
        json={"status": "in_progress"},
    )

    # Simulate a process restart: drop all in-memory ORM state and read the
    # audit back through a brand-new session straight from the database.
    db_session.expire_all()
    from app.database import SessionLocal
    from app.services import audit as audit_svc

    fresh = SessionLocal()
    try:
        events = audit_svc.list_events(fresh, ticket_id=ticket_id)
        actions = [e.action for e in events]
        assert actions == ["ticket.create", "ticket.assign", "ticket.transition"]
    finally:
        fresh.close()

    # Same ordering over HTTP.
    audit_res = client.get(f"/api/tickets/{ticket_id}/audit", headers=agent_headers)
    assert [e["action"] for e in audit_res.json()] == [
        "ticket.create",
        "ticket.assign",
        "ticket.transition",
    ]


def test_audit_details_never_store_secrets(db_session):
    from app.models import Ticket
    from app.services import audit as audit_svc

    ticket = Ticket(
        customer_id=1,
        subject="s",
        description="long enough description here",
        status="open",
    )
    db_session.add(ticket)
    db_session.commit()
    event = audit_svc.log_event(
        db_session,
        action="ticket.assign",
        ticket_id=ticket.id,
        details={"assignee_id": 2, "token": "secret-jwt", "auth_token": "x", "notes": "ok"},
    )
    db_session.commit()
    import json as _json

    stored = _json.loads(event.details)
    assert "token" not in stored and "auth_token" not in stored
    assert stored["notes"] == "ok"
