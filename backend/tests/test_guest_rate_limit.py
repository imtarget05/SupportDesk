"""Per-IP rate limit on anonymous (guest) ticket submission.

`POST /api/tickets` accepts submissions without an account by design, so the
only identity available is the peer address. Without a limit, that is an
unauthenticated row-creation primitive: anyone can fill the `tickets` table
(and the `users` table, via find-or-create) at arbitrary rate.
"""

import dataclasses

import pytest

from app import rate_limit
from app.api import tickets as tickets_api
from app.config import settings
from app.models import Ticket, User
from tests.conftest import create_ticket


@pytest.fixture()
def strict_limit(monkeypatch):
    """Collapse the guest allowance to 2 requests for the duration of a test."""
    tight = dataclasses.replace(settings, ticket_create_rate_limit=2)
    monkeypatch.setattr(tickets_api, "settings", tight)
    return tight


def test_guest_submissions_are_rate_limited(client, strict_limit):
    assert create_ticket(client, email="one@example.com").status_code == 201
    assert create_ticket(client, email="two@example.com").status_code == 201

    blocked = create_ticket(client, email="three@example.com")
    assert blocked.status_code == 429
    assert "retry in" in blocked.json()["detail"]
    assert int(blocked.headers["Retry-After"]) > 0


def test_rate_limited_submission_creates_no_rows(client, db_session, strict_limit):
    for i in range(2):
        assert create_ticket(client, email=f"ok{i}@example.com").status_code == 201
    assert create_ticket(client, email="blocked@example.com").status_code == 429

    assert db_session.query(Ticket).filter(Ticket.subject == "Printer on fire").count() == 2
    assert db_session.query(User).filter(User.email == "blocked@example.com").count() == 0


def test_authenticated_customers_are_not_rate_limited(client, customer_headers, strict_limit):
    """Authenticated submitters are attributable to an account and are not
    charged against the anonymous per-IP budget."""
    for _ in range(5):
        res = create_ticket(client, headers=customer_headers)
        assert res.status_code == 201, res.text


def test_schema_invalid_guest_submissions_do_not_consume_budget(client, strict_limit):
    """A body rejected by schema validation creates nothing and is therefore
    not charged against the guest budget.

    FastAPI validates the request body before the route handler runs, so the
    limit cannot see these requests at all. That is the intended behaviour: the
    control bounds row creation, and a 422 writes no rows.
    """
    payload = {"subject": "hi", "description": "way too short", "customer_email": "a@b.co"}
    for _ in range(4):
        assert client.post("/api/tickets", json=payload).status_code == 422

    # Budget untouched: a valid guest submission is still served.
    assert create_ticket(client, email="still-allowed@example.com").status_code == 201
    assert create_ticket(client, email="second@example.com").status_code == 201
    assert create_ticket(client, email="third@example.com").status_code == 429


def test_default_limit_allows_normal_guest_usage(client):
    """The shipped default is generous enough for ordinary contact-form use."""
    for i in range(5):
        assert create_ticket(client, email=f"guest{i}@example.com").status_code == 201


def test_zero_limit_disables_the_limiter():
    off = dataclasses.replace(settings, ticket_create_rate_limit=0)
    rate_limit.check("k", limit=off.ticket_create_rate_limit, window_s=60)
    rate_limit.check("k", limit=off.ticket_create_rate_limit, window_s=60)