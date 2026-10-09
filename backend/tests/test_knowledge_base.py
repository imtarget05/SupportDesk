"""Tests for the LlamaIndex knowledge base service."""

import pytest

from app.services.knowledge_base import (
    Evidence,
    KnowledgeBase,
    get_knowledge_base,
    reset_knowledge_base,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    yield
    reset_knowledge_base()


class TestEvidence:
    def test_evidence_creation(self):
        ev = Evidence(content="Test content", source="test.md", score=0.9)
        assert ev.content == "Test content"
        assert ev.source == "test.md"
        assert ev.score == 0.9
        assert ev.metadata is None

    def test_evidence_with_metadata(self):
        ev = Evidence(
            content="Test",
            source="test.md",
            metadata={"filename": "test.md", "type": "md"},
        )
        assert ev.metadata["filename"] == "test.md"


class TestKnowledgeBase:
    def test_init_default_dir(self):
        kb = KnowledgeBase()
        assert "knowledge" in kb._knowledge_dir

    def test_init_custom_dir(self, tmp_path):
        kb = KnowledgeBase(str(tmp_path))
        assert kb._knowledge_dir == str(tmp_path)

    def test_ingest_nonexistent_dir(self):
        kb = KnowledgeBase("/nonexistent/path")
        count = kb.ingest()
        assert count == 0

    def test_ingest_empty_dir(self, tmp_path):
        kb = KnowledgeBase(str(tmp_path))
        count = kb.ingest()
        assert count == 0

    def test_ingest_with_files(self, tmp_path):
        # Create test knowledge files
        (tmp_path / "faq.md").write_text("# FAQ\n\nHow do I reset my password?")
        (tmp_path / "guide.txt").write_text("Troubleshooting guide content.")

        kb = KnowledgeBase(str(tmp_path))
        count = kb.ingest()
        assert count == 2
        assert len(kb._documents) == 2

    def test_ingest_json_file(self, tmp_path):
        import json

        data = {"question": "How to login?", "answer": "Use your email and password."}
        (tmp_path / "data.json").write_text(json.dumps(data))

        kb = KnowledgeBase(str(tmp_path))
        count = kb.ingest()
        assert count == 1

    def test_ingest_ignores_unsupported_files(self, tmp_path):
        (tmp_path / "test.md").write_text("# Test")
        (tmp_path / "test.pdf").write_text("PDF content")
        (tmp_path / "test.doc").write_text("DOC content")

        kb = KnowledgeBase(str(tmp_path))
        count = kb.ingest()
        assert count == 1  # Only .md file

    def test_retrieve_fallback(self, tmp_path):
        (tmp_path / "faq.md").write_text(
            "# FAQ\n\nPayment issues can be resolved by contacting support."
        )

        kb = KnowledgeBase(str(tmp_path))
        results = kb.retrieve("payment issue", top_k=3)

        # Should find at least one result using fallback
        assert len(results) > 0
        assert any("payment" in r.content.lower() for r in results)

    def test_retrieve_empty_kb(self):
        kb = KnowledgeBase("/nonexistent/path")
        results = kb.retrieve("test query")
        assert results == []

    def test_get_stats(self, tmp_path):
        (tmp_path / "test.md").write_text("# Test")

        kb = KnowledgeBase(str(tmp_path))
        kb.ingest()

        stats = kb.get_stats()
        assert stats["documents_loaded"] == 1
        assert stats["knowledge_dir"] == str(tmp_path)

    def test_get_stats_empty(self):
        kb = KnowledgeBase("/nonexistent/path")
        stats = kb.get_stats()
        assert stats["documents_loaded"] == 0
        assert stats["index_built"] is False


class TestSingleton:
    def test_get_knowledge_base(self):
        kb = get_knowledge_base()
        assert isinstance(kb, KnowledgeBase)

    def test_reset_knowledge_base(self):
        kb1 = get_knowledge_base()
        reset_knowledge_base()
        kb2 = get_knowledge_base()
        assert kb1 is not kb2


class TestRealisticSOPKnowledgeBase:
    def test_ingest_and_retrieve_sops(self):
        kb = KnowledgeBase()
        count = kb.ingest(force=True)
        # Should have ingested all real SOP docs (>= 20)
        assert count >= 20

        # Test refund retrieval
        refund_results = kb.retrieve("How do I request a refund for an annual subscription?", top_k=3)
        assert len(refund_results) > 0
        assert any("refund" in r.content.lower() or "billing" in r.source.lower() for r in refund_results)

        # Test 2FA retrieval
        two_fa_results = kb.retrieve("lost authenticator app two factor recovery codes", top_k=3)
        assert len(two_fa_results) > 0
        assert any("factor" in r.content.lower() or "2fa" in r.content.lower() for r in two_fa_results)