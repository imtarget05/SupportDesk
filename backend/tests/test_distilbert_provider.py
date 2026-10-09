"""DistilBertProvider contract: the locally fine-tuned model actually serves.

The fine-tune itself lives in evaluation/train_transformer.py (exploration
deps, intentionally NOT in requirements.txt). This suite proves the serving
path: AI_PROVIDER=distilbert selects the provider and, when the (gitignored)
artifact exists, analyze() returns a validated AnalysisResult.

Run after training:
    cd backend && .venv/bin/python -c "from evaluation..."  # see evaluation/README.md
"""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]  # backend/
sys.path.insert(0, str(BACKEND))
ARTIFACT = BACKEND.parent / "evaluation" / "artifacts" / "distilbert"


def test_ai_provider_env_selects_distilbert_provider(monkeypatch):
    """AI_PROVIDER=distilbert must select DistilBertProvider (lazy-loaded)."""
    from types import SimpleNamespace

    from app.services import ai_service

    monkeypatch.setattr(ai_service, "settings", SimpleNamespace(ai_provider="distilbert"))
    monkeypatch.setattr(ai_service, "_provider", None)
    provider = ai_service.get_provider()
    assert isinstance(provider, ai_service.DistilBertProvider)


def test_artifact_missing_raises_ai_provider_error(monkeypatch, tmp_path):
    """No artifact -> a clear AIProviderError, never a silent stub fallback."""
    from app.services import ai_service

    provider = ai_service.DistilBertProvider()
    monkeypatch.setattr(provider, "_artifact", tmp_path / "does-not-exist")
    with pytest.raises(ai_service.AIProviderError, match="artifact not found"):
        provider.analyze("subject", "description")


def test_suggest_does_not_need_the_artifact():
    """Drafting delegates to the deterministic stub — no torch import needed."""
    from app.services.ai_service import DistilBertProvider

    draft = DistilBertProvider().suggest("Cannot login", "I am locked out.", "")
    assert isinstance(draft, str) and len(draft) > 0


def test_analyze_serves_real_predictions_when_artifact_exists():
    """End-to-end serve: fine-tuned weights -> validated AnalysisResult."""
    if not (ARTIFACT / "config.json").exists():
        pytest.skip("DistilBERT artifact not present (gitignored); train via evaluation/train_transformer.py")
    pytest.importorskip("torch")
    pytest.importorskip("transformers")

    from app.enums import TicketCategory, TicketPriority
    from app.services.ai_service import AnalysisResult, DistilBertProvider

    provider = DistilBertProvider()
    result = provider.analyze(
        "Cannot login after password reset",
        "I reset my password but now I cannot sign in, account is locked.",
    )
    assert isinstance(result, AnalysisResult)
    assert isinstance(result.category, TicketCategory)
    assert isinstance(result.priority, TicketPriority)
    assert 0.0 <= result.confidence <= 1.0
    assert result.summary
    assert provider.last_usage.total_tokens > 0
