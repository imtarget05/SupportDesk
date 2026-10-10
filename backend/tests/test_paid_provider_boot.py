"""``AI_PROVIDER=openai`` (DECISIONS.md D5) must be safe to pin in render.yaml.

The provider is constructed lazily on the first AI call, so deploying the
blueprint with ``AI_PROVIDER=openai`` and an empty ``OPENAI_API_KEY`` cannot
stop the service from booting: only the AI endpoints answer, and they answer
with a clear 502 instead of a crash.
"""

import dataclasses
import re
from pathlib import Path

import pytest
from fastapi import status

from app.config import settings
from app.services import ai_service, budget


@pytest.fixture(autouse=True)
def _reset_provider_cache():
    """The provider is cached process-wide; drop it around every test."""
    ai_service.set_provider(None)
    yield
    ai_service.set_provider(None)


@pytest.fixture()
def openai_without_key(monkeypatch):
    """Frozen dataclass -> replace, then patch the name each module uses."""
    patched = dataclasses.replace(settings, ai_provider="openai", openai_api_key="")
    monkeypatch.setattr(ai_service, "settings", patched)
    monkeypatch.setattr(budget, "settings", patched)
    return patched


def test_openai_without_key_is_only_broken_at_call_time(openai_without_key):
    """Import/boot never touches the key — get_provider() does, lazily."""
    with pytest.raises(ai_service.AIProviderError, match="OPENAI_API_KEY is not set"):
        ai_service.get_provider()


def test_missing_key_maps_to_502_and_a_clear_message(openai_without_key):
    from app.api.ai import _ai_error

    with pytest.raises(ai_service.AIProviderError) as excinfo:
        ai_service.get_provider()

    http_exc = _ai_error(excinfo.value)
    assert http_exc.status_code == status.HTTP_502_BAD_GATEWAY
    assert "OPENAI_API_KEY" in http_exc.detail


def test_budget_cap_still_applies_to_the_paid_openai_provider():
    assert budget.is_paid_provider("openai") is True
    assert budget.is_paid_provider("stub") is False


def test_blueprint_pins_openai_so_no_dashboard_flip_is_needed():
    render_yaml = Path(__file__).resolve().parents[2] / "render.yaml"
    text = render_yaml.read_text()

    match = re.search(r"- key: AI_PROVIDER\s*\n\s+value: (\S+)", text)
    assert match, "render.yaml must pin AI_PROVIDER"
    assert match.group(1) == "openai"
    assert "sync: false" in text and "OPENAI_API_KEY" in text
