"""Vector store abstraction for knowledge-base retrieval.

Two backends behind one interface:

  LlamaIndex (in-process, the default)  needs no extra service
  Qdrant                                what a deployment scales to

The abstraction exists so switching stores does not change the workflow. Qdrant
is reachable in three ways, tried in order, because development should not need
a running server:

  1. ``QDRANT_URL``  a real Qdrant server
  2. ``QDRANT_PATH`` embedded and on-disk, persists between runs
  3. ``:memory:``    embedded and in-process, discarded on exit

If none of those work the store reports itself unavailable and retrieval falls
back to LlamaIndex, so a misconfigured QDRANT_URL degrades retrieval quality
rather than breaking the feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.knowledge_base import Evidence, get_knowledge_base
from app.services.retrieval_service import embed_text, get_embed_dim

logger = logging.getLogger(__name__)

CHUNK_SIZE = 512
CHUNK_OVERLAP = 50


@dataclass
class Chunk:
    """One indexed piece of a knowledge document."""

    id: str
    text: str
    source: str
    metadata: dict[str, Any]


def chunk_documents(documents: list[dict[str, Any]]) -> list[Chunk]:
    """Split ingested documents into overlapping chunks with stable ids.

    The id hashes the source and offset, so re-ingesting unchanged content
    produces the same ids and upserts stay idempotent.
    """
    import hashlib

    chunks: list[Chunk] = []
    for doc in documents:
        text = doc.get("content", "")
        source = doc.get("source", "unknown")
        start = 0
        index = 0
        while True:
            piece = text[start : start + CHUNK_SIZE]
            if not piece.strip() and index > 0:
                break
            digest = hashlib.sha256(f"{source}:{start}".encode()).hexdigest()[:32]
            chunks.append(
                Chunk(
                    id=digest,
                    text=piece,
                    source=source,
                    metadata={**(doc.get("metadata") or {}), "chunk_index": index},
                )
            )
            if start + CHUNK_SIZE >= len(text):
                break
            start += CHUNK_SIZE - CHUNK_OVERLAP
            index += 1
    return chunks
class QdrantVectorStore:
    """Knowledge-base chunks stored in Qdrant."""

    def __init__(self) -> None:
        self._collection = settings.qdrant_collection
        self._dim = get_embed_dim()
        self._client = self._connect()
        self._ensure_collection()
        self._indexed = False

    def _connect(self):
        from qdrant_client import QdrantClient

        if settings.qdrant_url:
            return QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key or None,
                timeout=5,
            )
        if settings.qdrant_path:
            Path(settings.qdrant_path).mkdir(parents=True, exist_ok=True)
            return QdrantClient(path=settings.qdrant_path)
        return QdrantClient(location=":memory:")

    def _ensure_collection(self) -> None:
        from qdrant_client.http import models

        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection in existing:
            return
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=models.VectorParams(
                size=self._dim, distance=models.Distance.COSINE
            ),
        )

    def index(self, force: bool = False) -> int:
        """Index the knowledge base; skips the work when already done."""
        if self._indexed and not force:
            return 0
        chunks = chunk_documents(get_knowledge_base().ingest_documents())
        if not chunks:
            self._indexed = True
            return 0

        from qdrant_client.http import models

        points = [
            models.PointStruct(
                id=chunk.id,
                vector=embed_text(chunk.text),
                payload={"text": chunk.text, "source": chunk.source, **chunk.metadata},
            )
            for chunk in chunks
        ]
        self._client.upsert(collection_name=self._collection, points=points)
        self._indexed = True
        return len(points)

    def retrieve(self, query: str, top_k: int = 3) -> list[Evidence]:
        """Nearest chunks to the query, as Evidence objects."""
        self.index()
        # qdrant-client 1.19 renamed `search()` to `query_points()`.
        response = self._client.query_points(
            collection_name=self._collection,
            query=embed_text(query),
            limit=top_k,
        )
        return [
            Evidence(
                content=hit.payload.get("text", ""),
                source=hit.payload.get("source", "unknown"),
                score=hit.score,
                metadata=hit.payload,
            )
            for hit in response.points
        ]

    def stats(self) -> dict[str, Any]:
        try:
            count = self._client.count(collection_name=self._collection).count
        except Exception:  # noqa: BLE001 — stats must not raise
            count = None
        return {
            "backend": "qdrant",
            "collection": self._collection,
            "dimensions": self._dim,
            "points": count,
        }


_store: QdrantVectorStore | None = None


def get_qdrant_store() -> QdrantVectorStore | None:
    """Build the Qdrant store once, or return ``None`` when it is unusable."""
    global _store
    if _store is not None:
        return _store
    try:
        _store = QdrantVectorStore()
    except Exception as exc:  # noqa: BLE001 — degrade to LlamaIndex instead
        logger.warning("Qdrant unavailable (%s); falling back to LlamaIndex", exc)
        return None
    return _store


def reset_qdrant_store() -> None:
    """Reset the singleton (for testing)."""
    global _store
    _store = None


def active_backend() -> str:
    """Which vector store retrieval will actually use, given the config."""
    if settings.ai_vector_store != "qdrant":
        return "llama_index"
    return "qdrant" if get_qdrant_store() is not None else "llama_index (qdrant unavailable)"