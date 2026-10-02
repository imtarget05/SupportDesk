"""Provider-selection and wire-protocol tests for the remote LLM backends."""

import dataclasses

import httpx
import pytest

from app import config as config_module
from app.services import ai_service


def _with_settings(**overrides):
    return dataclasses.replace(config_module.settings, **overrides)


ANTHROPIC_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
OPENAI_REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _anthropic_response(status: int, payload: dict) -> httpx.Response:
    """Build a response with a request attached.

    `httpx.Response.raise_for_status` needs a request; a bare response raises
    RuntimeError instead of the HTTP error under test.
    """
    return httpx.Response(status, json=payload, request=ANTHROPIC_REQ)


def test_anthropic_provider_requires_key(monkeypatch):
    monkeypatch.setattr(ai_service, "settings", _with_settings(anthropic_api_key=""))
    with pytest.raises(ai_service.AIProviderError, match="ANTHROPIC_API_KEY"):
        ai_service.AnthropicProvider()


def test_anthropic_selected_by_provider_setting(monkeypatch):
    ai_service.set_provider(None)
    monkeypatch.setattr(
        ai_service, "settings", _with_settings(ai_provider="anthropic", anthropic_api_key="sk-test")
    )
    assert isinstance(ai_service.get_provider(), ai_service.AnthropicProvider)


def test_anthropic_parses_usage_and_text(monkeypatch):
    """A successful Anthropic response yields text and real (not estimated) usage."""
    from app.enums import TicketCategory

    monkeypatch.setattr(
        ai_service,
        "settings",
        _with_settings(anthropic_api_key="sk-test", anthropic_model="claude-sonnet-4-5"),
    )
    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return _anthropic_response(
            200,
            {
                "content": [
                    {
                        "type": "text",
                        "text": '{"category": "payment", "priority": "high", "summary": "s", "confidence": 0.9}',
                    }
                ],
                "usage": {"input_tokens": 1200, "output_tokens": 34},
            },
        )

    monkeypatch.setattr(ai_service.httpx, "post", fake_post)
    provider = ai_service.AnthropicProvider()
    result = provider.analyze("Card charged twice", "Charged twice.")

    assert result.category == TicketCategory.PAYMENT
    assert provider.last_usage.prompt_tokens == 1200
    assert provider.last_usage.completion_tokens == 34
    assert provider.last_usage.estimated is False
    # Untrusted ticket text must not leak into the system prompt slot.
    assert captured["json"]["system"].startswith("You are a support-ticket triage")


def test_anthropic_rate_limit_is_transient(monkeypatch):
    monkeypatch.setattr(ai_service, "settings", _with_settings(anthropic_api_key="sk-test"))
    monkeypatch.setattr(
        ai_service.httpx, "post",
        lambda url, **kwargs: _anthropic_response(429, {"error": "slow down"}),
    )
    with pytest.raises(ai_service.TransientAIProviderError):
        ai_service.AnthropicProvider().suggest("hi", "there", "")


def test_anthropic_empty_response_is_an_error(monkeypatch):
    monkeypatch.setattr(ai_service, "settings", _with_settings(anthropic_api_key="sk-test"))
    monkeypatch.setattr(
        ai_service.httpx, "post",
        lambda url, **kwargs: _anthropic_response(200, {"content": []}),
    )
    with pytest.raises(ai_service.AIProviderError, match="Empty Anthropic response"):
        ai_service.AnthropicProvider().suggest("hi", "there", "")


def test_openai_provider_reads_usage(monkeypatch):
    monkeypatch.setattr(
        ai_service, "settings", _with_settings(openai_api_key="sk-test", ai_model="gpt-4o-mini")
    )
    monkeypatch.setattr(
        ai_service.httpx, "post",
        lambda url, **kwargs: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 7},
            },
            request=OPENAI_REQ,
        ),
    )
    provider = ai_service.OpenAIProvider()
    assert provider._chat("sys", "user", json_mode=False) == "hello"
    assert provider.last_usage.prompt_tokens == 500
    assert provider.last_usage.estimated is False


def test_openai_missing_usage_falls_back_to_estimate(monkeypatch):
    monkeypatch.setattr(ai_service, "settings", _with_settings(openai_api_key="sk-test"))
    monkeypatch.setattr(
        ai_service.httpx, "post",
        lambda url, **kwargs: httpx.Response(
            200,
            json={"choices": [{"message": {"content": "hi"}}]},
            request=OPENAI_REQ,
        ),
    )
    provider = ai_service.OpenAIProvider()
    provider._chat("sys", "user", json_mode=False)
    assert provider.last_usage.estimated is True
    assert provider.last_usage.total_tokens > 0
