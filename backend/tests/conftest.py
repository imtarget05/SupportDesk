"""Test setup: isolated SQLite DB per test, no external services required."""

import os
import secrets
import tempfile

# Must be set before importing app modules (config reads env at import time).
_TMP_DIR = tempfile.mkdtemp(prefix="supportdesk-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DIR}/test.db"
os.environ["JWT_SECRET"] = secrets.token_urlsafe(32)
os.environ["AI_PROVIDER"] = "stub"
os.environ.setdefault("EMAIL_PROVIDER", "stub")
os.environ.setdefault("SMTP_HOST", "")
os.environ.setdefault("SMTP_FROM", "")

import pytest
from fastapi.testclient import TestClient

from app import rate_limit
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.security import hash_password


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Rate-limit buckets are process-global; clear them between tests."""
    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(db_session):
    with TestClient(app) as c:
        yield c


def make_user(db, *, email: str, name: str, role: str, password: str | None = "pw1234") -> User:
    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password) if password else None,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture()
def agent_user(db_session):
    return make_user(
        db_session, email="agent@test.dev", name="Test Agent", role="agent", password="agentpw"
    )


@pytest.fixture()
def customer_user(db_session):
    return make_user(
        db_session, email="cust@test.dev", name="Test Customer", role="customer", password="custpw"
    )


def login(client, email: str, password: str) -> dict:
    """Return Authorization headers for the given credentials."""
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


@pytest.fixture()
def agent_headers(client, agent_user):
    return login(client, "agent@test.dev", "agentpw")


@pytest.fixture()
def customer_headers(client, customer_user):
    return login(client, "cust@test.dev", "custpw")


def create_ticket(client, *, subject="Printer on fire", email=None, headers=None, description=None):
    payload = {
        "subject": subject,
        "description": description or "The office printer started smoking during a print job.",
    }
    if headers is None:
        payload["customer_email"] = email or "anon@example.com"
        payload["customer_name"] = "Anon User"
    return client.post("/api/tickets", json=payload, headers=headers)
