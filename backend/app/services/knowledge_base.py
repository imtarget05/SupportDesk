"""LlamaIndex knowledge base for RAG (company docs, FAQ, SOP, troubleshooting).

This module provides:
    - Document ingestion from a knowledge directory
    - Vector index construction
    - Retriever for relevant evidence given a query

The knowledge base is used by the ticket processing pipeline to retrieve relevant
context before drafting a response. Retrieved evidence includes source
metadata for citation and audit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Evidence:
    """A piece of retrieved evidence with source metadata."""
    content: str
    source: str
    score: float | None = None
    metadata: dict[str, Any] | None = None


class KnowledgeBase:
    """LlamaIndex-backed knowledge base for support documentation.

    Supports ingestion of text/markdown documents and retrieval of
    relevant evidence for a given query.
    """

    def __init__(self, knowledge_dir: str | None = None):
        self._knowledge_dir = knowledge_dir or os.getenv(
            "KNOWLEDGE_DIR", str(Path(__file__).resolve().parent.parent.parent / "knowledge")
        )
        self._index = None
        self._retriever = None
        self._documents: list[dict[str, Any]] = []

    def ingest(self, force: bool = False) -> int:
        """Ingest documents from the knowledge directory."""
        if self._index is not None and not force:
            return len(self._documents)

        knowledge_path = Path(self._knowledge_dir)
        if not knowledge_path.exists():
            logger.warning("Knowledge directory not found: %s", self._knowledge_dir)
            return 0

        self._documents = []
        for file_path in knowledge_path.rglob("*"):
            if file_path.is_file() and file_path.suffix in (".txt", ".md", ".json"):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    doc = {
                        "content": content,
                        "source": str(file_path.relative_to(knowledge_path)),
                        "metadata": {
                            "filename": file_path.name,
                            "path": str(file_path),
                            "type": file_path.suffix.lstrip("."),
                        },
                    }
                    self._documents.append(doc)
                except Exception as exc:
                    logger.warning("Failed to read %s: %s", file_path, exc)

        if self._documents:
            self._build_index()

        return len(self._documents)

    def _build_index(self) -> None:
        """Build the LlamaIndex vector index from ingested documents."""
        try:
            from llama_index.core import Document, VectorStoreIndex, StorageContext
            from llama_index.core.node_parser import SentenceSplitter
            from llama_index.embeddings.huggingface import HuggingFaceEmbedding

            documents = [
                Document(text=doc["content"], metadata=doc["metadata"])
                for doc in self._documents
            ]

            embed_model = HuggingFaceEmbedding(
                model_name="sentence-transformers/all-MiniLM-L6-v2"
            )

            node_parser = SentenceSplitter(chunk_size=512, chunk_overlap=50)
            nodes = node_parser.get_nodes_from_documents(documents)

            storage_context = StorageContext.from_defaults()
            self._index = VectorStoreIndex(
                nodes=nodes,
                storage_context=storage_context,
                embed_model=embed_model,
            )
            self._retriever = self._index.as_retriever(similarity_top_k=3)
            logger.info("Knowledge base index built with %d nodes", len(nodes))

        except ImportError:
            logger.warning("LlamaIndex not available; using fallback retrieval")
            self._index = None
            self._retriever = None

    def retrieve(self, query: str, top_k: int = 3) -> list[Evidence]:
        """Retrieve relevant evidence for a query."""
        if not self._documents:
            self.ingest()

        if not self._documents:
            return []

        if self._retriever is not None:
            return self._retrieve_with_index(query, top_k)

        return self._retrieve_fallback(query, top_k)

    def _retrieve_with_index(self, query: str, top_k: int) -> list[Evidence]:
        """Retrieve using LlamaIndex vector index."""
        try:
            from llama_index.core.schema import NodeWithScore

            results: list[NodeWithScore] = self._retriever.retrieve(query)
            evidence = []
            for node_with_score in results[:top_k]:
                node = node_with_score.node
                evidence.append(Evidence(
                    content=node.get_content(),
                    source=node.metadata.get("source", node.metadata.get("filename", "unknown")),
                    score=node_with_score.score,
                    metadata=node.metadata,
                ))
            return evidence
        except Exception as exc:
            logger.warning("LlamaIndex retrieval failed: %s", exc)
            return self._retrieve_fallback(query, top_k)

    def _retrieve_fallback(self, query: str, top_k: int) -> list[Evidence]:
        """Fallback retrieval using simple keyword matching."""
        query_terms = set(query.lower().split())
        scored = []
        for doc in self._documents:
            content_lower = doc["content"].lower()
            score = sum(1 for term in query_terms if term in content_lower)
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            Evidence(
                content=doc["content"][:1000],
                source=doc["source"],
                score=score / max(len(query_terms), 1),
                metadata=doc["metadata"],
            )
            for score, doc in scored[:top_k]
        ]

    def get_stats(self) -> dict[str, Any]:
        """Return statistics about the knowledge base."""
        return {
            "documents_loaded": len(self._documents),
            "index_built": self._index is not None,
            "knowledge_dir": self._knowledge_dir,
        }


_knowledge_base: KnowledgeBase | None = None


def get_knowledge_base() -> KnowledgeBase:
    """Get or create the singleton KnowledgeBase instance."""
    global _knowledge_base
    if _knowledge_base is None:
        _knowledge_base = KnowledgeBase()
    return _knowledge_base


def reset_knowledge_base() -> None:
    """Reset the singleton (for testing)."""
    global _knowledge_base
    _knowledge_base = None
