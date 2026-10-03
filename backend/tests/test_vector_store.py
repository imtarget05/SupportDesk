"""Qdrant vector store: chunking, indexing, retrieval, and graceful fallback.

Runs against the embedded in-memory client, so no Qdrant server is needed and
the suite stays offline.
"""

import dataclasses

import pytest

from app import config as config_module
from app.services import vector_store as vs
from app.services.knowledge_base import KnowledgeBase, reset_knowledge_base


@pytest.fixture(autouse=True)
def _reset_store():
    yield
    vs.reset_qdrant_store()


@pytest.fixture()
def qdrant_settings(monkeypatch):
    """Point the config at an embedded, in-memory Qdrant.

    Patches `vector_store.settings`, not `config.settings`: this module does
    `from app.config import settings`, which binds a local name that patching
    the source module cannot reach.
    """
    monkeypatch.setattr(
        vs, "settings",
        dataclasses.replace(
            vs.settings,
            ai_vector_store="qdrant",
            qdrant_url="",
            qdrant_path="",
            qdrant_collection="test_kb",
        ),
    )
    vs.reset_qdrant_store()
    yield vs.settings


def _kb(monkeypatch, tmp_path, name="policy.md", body="Refunds within 30 days."):
    """Point the knowledge base at a single temp document."""
    knowledge = tmp_path / "kb"
    knowledge.mkdir()
    (knowledge / name).write_text(body)
    reset_knowledge_base()
    store = KnowledgeBase(str(knowledge))
    monkeypatch.setattr(vs, "get_knowledge_base", lambda: store)
    return store


# ---------------------------------------------------------------- chunking


def test_chunking_splits_long_documents():
    docs = [{"content": "a" * 2000, "source": "long.md", "metadata": {}}]
    chunks = vs.chunk_documents(docs)
    assert len(chunks) > 1
    assert all(len(c.text) <= vs.CHUNK_SIZE for c in chunks)


def test_chunking_keeps_short_documents_whole():
    docs = [{"content": "short policy text", "source": "s.md", "metadata": {}}]
    chunks = vs.chunk_documents(docs)
    assert len(chunks) == 1
    assert chunks[0].text == "short policy text"


def test_chunk_ids_are_stable_across_runs():
    """Stable ids make re-ingesting idempotent rather than duplicating points."""
    docs = [{"content": "b" * 900, "source": "x.md", "metadata": {}}]
    assert [c.id for c in vs.chunk_documents(docs)] == [
        c.id for c in vs.chunk_documents(docs)
    ]


def test_chunk_ids_differ_per_source():
    docs_a = [{"content": "same text here", "source": "a.md", "metadata": {}}]
    docs_b = [{"content": "same text here", "source": "b.md", "metadata": {}}]
    assert vs.chunk_documents(docs_a)[0].id != vs.chunk_documents(docs_b)[0].id


def test_chunking_carries_metadata_through():
    docs = [{"content": "text", "source": "s.md", "metadata": {"filename": "s.md"}}]
    chunk = vs.chunk_documents(docs)[0]
    assert chunk.metadata["filename"] == "s.md"
    assert chunk.metadata["chunk_index"] == 0


def test_chunking_handles_empty_input():
    assert vs.chunk_documents([]) == []
# ------------------------------------------------------- indexing & retrieval


def test_index_returns_a_point_count(qdrant_settings, monkeypatch, tmp_path):
    _kb(monkeypatch, tmp_path)
    assert vs.get_qdrant_store().index() >= 1


def test_index_is_idempotent(qdrant_settings, monkeypatch, tmp_path):
    _kb(monkeypatch, tmp_path)
    store = vs.get_qdrant_store()
    first = store.index()
    assert store.index() == 0, "a second index call should be a no-op"
    assert store.index(force=True) == first


def test_retrieve_returns_scored_evidence(qdrant_settings, monkeypatch, tmp_path):
    _kb(
        monkeypatch,
        tmp_path,
        name="refund.md",
        body="Refunds for physical products are accepted within 30 days of delivery.",
    )
    evidence = vs.get_qdrant_store().retrieve("how long is the refund window", top_k=2)
    assert evidence
    assert all(e.score is not None for e in evidence)
    assert all(-0.01 <= e.score <= 1.01 for e in evidence)
    assert evidence[0].source == "refund.md"


def test_retrieve_respects_top_k(qdrant_settings, monkeypatch, tmp_path):
    knowledge = tmp_path / "kb"
    knowledge.mkdir()
    for i in range(5):
        (knowledge / f"doc{i}.md").write_text(f"Policy {i} about refunds and returns.")
    reset_knowledge_base()
    monkeypatch.setattr(vs, "get_knowledge_base", lambda: KnowledgeBase(str(knowledge)))
    assert len(vs.get_qdrant_store().retrieve("refund", top_k=2)) == 2


def test_stats_report_the_backend(qdrant_settings):
    stats = vs.get_qdrant_store().stats()
    assert stats["backend"] == "qdrant"
    assert stats["collection"] == "test_kb"
    assert stats["dimensions"] > 0


# ---------------------------------------------------------------- fallback


def test_llama_index_is_the_default_backend():
    assert vs.active_backend() == "llama_index"


def test_qdrant_is_reported_when_selected(qdrant_settings):
    assert vs.active_backend() == "qdrant"


def test_unreachable_qdrant_falls_back_instead_of_raising(monkeypatch):
    """A misconfigured QDRANT_URL degrades retrieval, never the request."""
    monkeypatch.setattr(
        vs, "settings",
        dataclasses.replace(
            vs.settings,
            ai_vector_store="qdrant",
            qdrant_url="http://127.0.0.1:1",
            qdrant_path="",
        ),
    )
    vs.reset_qdrant_store()
    assert vs.get_qdrant_store() is None
    assert "llama_index" in vs.active_backend()


def test_knowledge_base_falls_back_when_qdrant_raises(monkeypatch, tmp_path):
    """Retrieval still works through LlamaIndex when the store misbehaves."""
    import app.services.knowledge_base as kb_module

    monkeypatch.setattr(
        kb_module, "settings",
        dataclasses.replace(kb_module.settings, ai_vector_store="qdrant"),
    )
    vs.reset_qdrant_store()

    knowledge = tmp_path / "kb2"
    knowledge.mkdir()
    (knowledge / "faq.md").write_text("Refunds are handled by the billing team.")

    class BrokenStore:
        def retrieve(self, query, top_k):
            raise RuntimeError("qdrant exploded")

    monkeypatch.setattr(vs, "get_qdrant_store", lambda: BrokenStore())
    kb = KnowledgeBase(str(knowledge))
    assert kb.retrieve("refund", top_k=2), "the LlamaIndex path must still answer"