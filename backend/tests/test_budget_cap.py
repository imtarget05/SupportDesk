"""Monthly AI budget cap: a $10 hard stop on paid provider calls.

The cap is money, enforced fail-closed before a paid call is made:

  * spend >= cap        -> BudgetExceededError (reason "cap_reached")
  * spend unreadable    -> BudgetExceededError (reason "spend_unavailable")
  * local providers     -> never capped (stub/distilbert cost nothing)
  * cap <= 0            -> explicitly disabled

Covered here: the enforcement itself, the refusal to run unmeasured, the
calendar-month window, the 429 the API answers with, and the budget block the
metrics endpoint exposes so the limit is visible before it bites.
"""

import dataclasses
from datetime import datetime

import pytest

from app import config as config_module
from app.models import AICallTrace
from app.services import ai_service, budget


class _SpyProvider(ai_service.StubProvider):
    """Stub behaviour (no network) with a call counter, standing in for a paid LLM."""

    model = "gpt-4o"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, subject, description):
        self.calls += 1
        return super().analyze(subject, description)

    def suggest(self, subject, description, thread):
        self.calls += 1
        return super().suggest(subject, description, thread)


@pytest.fixture
def paid(monkeypatch):
    """Install paid-provider settings (openai + a $10 cap) for one test."""

    def apply(**overrides):
        defaults = {"ai_provider": "openai", "ai_monthly_budget_usd": 10.0}
        defaults.update(overrides)
        patched = dataclasses.replace(config_module.settings, **defaults)
        monkeypatch.setattr(ai_service, "settings", patched)
        monkeypatch.setattr(budget, "settings", patched)
        return patched

    return apply


def _seed_spend(db, cost_usd, *, created_at=None) -> None:
    row = AICallTrace(
        request_id="seed0000000000001",
        operation="triage",
        provider="openai",
        model="gpt-4o",
        prompt_tokens=1000,
        completion_tokens=500,
        cost_usd=cost_usd,
        latency_ms=100,
        outcome="ok",
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()


def test_cap_reached_refuses_paid_call_before_the_provider_runs(paid, db_session):
    paid()
    _seed_spend(db_session, 10.0)
    spy = _SpyProvider()
    ai_service.set_provider(spy)

    with pytest.raises(ai_service.BudgetExceededError) as excinfo:
        ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")

    assert excinfo.value.reason == "cap_reached"
    assert "$10.00" in str(excinfo.value)
    assert spy.calls == 0, "the provider must not be called once the cap is hit"


def test_under_cap_the_paid_call_goes_through(paid, db_session):
    paid()
    _seed_spend(db_session, 1.0)
    spy = _SpyProvider()
    ai_service.set_provider(spy)

    result = ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")

    assert spy.calls == 1
    assert 0.0 <= result.confidence <= 1.0


def test_cap_boundary_is_reached_not_exceeded(paid, db_session):
    """Spending exactly the cap blocks: the budget is not an allowance to overshoot."""
    paid()
    _seed_spend(db_session, 9.999)
    ai_service.set_provider(_SpyProvider())

    # 9.999 < 10.0 -> allowed
    ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")

    _seed_spend(db_session, 0.001)
    ai_service.set_provider(_SpyProvider())
    with pytest.raises(ai_service.BudgetExceededError):
        ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")


def test_local_stub_provider_is_never_capped(db_session, monkeypatch):
    """Free providers keep working at any spend: dev and tests need no budget."""
    monkeypatch.setattr(
        budget, "settings", dataclasses.replace(config_module.settings, ai_monthly_budget_usd=1.0)
    )
    _seed_spend(db_session, 99.0)
    spy = _SpyProvider()
    ai_service.set_provider(spy)

    result = ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")

    assert spy.calls == 1
    assert result.summary


def test_zero_cap_disables_enforcement(paid, db_session):
    paid(ai_monthly_budget_usd=0)
    _seed_spend(db_session, 100.0)
    spy = _SpyProvider()
    ai_service.set_provider(spy)

    ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")

    assert spy.calls == 1
    assert budget.budget_cap_usd() is None


def test_unreadable_spend_fails_closed(paid, db_session, monkeypatch):
    """No measurement means no call: an unenforced budget is not a budget."""
    paid()
    ai_service.set_provider(_SpyProvider())

    def _boom(*args, **kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(budget, "monthly_spend_usd", _boom)

    with pytest.raises(ai_service.BudgetExceededError) as excinfo:
        ai_service.suggest_response("Printer on fire", "Smoke", "customer: hello")

    assert excinfo.value.reason == "spend_unavailable"
    assert isinstance(excinfo.value, ai_service.AIProviderError)


def test_only_the_current_calendar_month_counts(paid, db_session):
    paid()
    _seed_spend(
        db_session,
        50.0,
        created_at=datetime(2000, 1, 15, 12, 0, 0),
    )
    assert budget.monthly_spend_usd(db_session) == 0.0

    spy = _SpyProvider()
    ai_service.set_provider(spy)
    ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")
    assert spy.calls == 1, "last month's spend must not block this month"


def test_refused_call_is_recorded_as_a_trace(paid, db_session):
    paid()
    _seed_spend(db_session, 10.0)
    ai_service.set_provider(_SpyProvider())

    with pytest.raises(ai_service.BudgetExceededError):
        ai_service.analyze_ticket("Printer on fire", "Smoke everywhere")

    refused = (
        db_session.query(AICallTrace)
        .filter(AICallTrace.error_kind == "BudgetExceededError")
        .one()
    )
    assert refused.outcome == "error"
    assert refused.operation == "triage"


def test_api_answers_429_when_the_cap_is_reached(
    paid, db_session, client, agent_headers, customer_headers
):
    paid()
    _seed_spend(db_session, 10.0)
    ai_service.set_provider(_SpyProvider())
    ticket_id = client.post(
        "/api/tickets",
        json={"subject": "Printer on fire", "description": "Smoke everywhere"},
        headers=customer_headers,
    ).json()["id"]

    res = client.post(f"/api/tickets/{ticket_id}/ai/analyze", headers=agent_headers)

    assert res.status_code == 429, res.text
    assert "budget" in res.json()["detail"].lower()
    # The refusal must leave the ticket untouched.
    fresh = client.get(f"/api/tickets/{ticket_id}", headers=agent_headers).json()
    assert fresh["ai_confidence"] in (None, 0)


def test_budget_state_is_visible_on_the_metrics_endpoint(client, agent_headers, db_session):
    res = client.get("/api/metrics/ai", headers=agent_headers)

    assert res.status_code == 200, res.text
    budget_block = res.json()["budget"]
    assert budget_block["cap_usd"] == 10.0
    assert budget_block["month_to_date_usd"] >= 0.0
    assert budget_block["provider"] == "stub"
    assert budget_block["paid_provider"] is False
