"""Pinning the paid provider in render.yaml (DECISIONS.md D5) must be boot-safe.

The blueprint ships ``AI_PROVIDER=cloudflare`` (Workers AI). Its provider class
is constructed lazily on the first AI call, so a deploy with credentials still
missing must not stop the service from booting: only the AI endpoints answer,
and they answer with a clear 502 naming the missing configuration.
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


@pytest.fixture
def cloudflare_without_credentials(monkeypatch):
    """Cloudflare selected but empty credentials.

    Settings is a frozen dataclass, so replace it wholesale and patch the name
    each module that reads it uses.
    """
    patched = dataclasses.replace(
        settings,
        ai_provider="cloudflare",
        cloudflare_account_id="",
        cloudflare_api_token="",
    )
    monkeypatch.setattr(ai_service, "settings", patched)
    monkeypatch.setattr(budget, "settings", patched)
    return patched


def test_cloudflare_without_credentials_is_only_broken_at_call_time(
    cloudflare_without_credentials,
):
    """Booting never touches the credentials — get_provider() does, lazily."""
    with pytest.raises(
        ai_service.AIProviderError,
        match="AI_PROVIDER=cloudflare requires CLOUDFLARE_ACCOUNT_ID"
    ):
        ai_service.get_provider()


def test_missing_credentials_map_to_502_and_a_clear_message(
    cloudflare_without_credentials,
):
    from app.api.ai import _ai_error

    with pytest.raises(ai_service.AIProviderError) as excinfo:
        ai_service.get_provider()

    http_exc = _ai_error(excinfo.value)
    assert http_exc.status_code == status.HTTP_502_BAD_GATEWAY
    assert "CLOUDFLARE_ACCOUNT_ID" in http_exc.detail


def test_budget_cap_still_applies_to_the_paid_cloudflare_provider():
    # Cloudflare Workers AI costs money, so the spend cap covers it (D5/D8).
    assert budget.is_paid_provider("cloudflare") is True
    assert budget.is_paid_provider("stub") is False


def test_blueprint_pins_cloudflare_so_no_dashboard_flip_is_needed():
    render_yaml = Path(__file__).resolve().parents[2] / "render.yaml"
    text = render_yaml.read_text()

    match = re.search(r"- key: AI_PROVIDER\s*\n\s+value: (\S+)", text)
    assert match is not None, "render.yaml must pin AI_PROVIDER"
    assert match.group(1) == "cloudflare"
    # Credentials stay out of the blueprint (sync: false); the model is pinned.
    assert "CLOUDFLARE_ACCOUNT_ID" in text and "CLOUDFLARE_API_TOKEN" in text
    assert text.count("- key: CLOUDFLARE_API_TOKEN\n        sync: false") == 1
    assert text.count("- key: CLOUDFLARE_MODEL") == 1
