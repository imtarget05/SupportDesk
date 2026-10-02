"""Similar-ticket retrieval (embedding + cosine similarity).

Embeddings are stored in `ticket_embeddings` as JSON float lists, alongside the
embedder id and width that produced them. Similarity runs in Python, which is
fine at MVP scale.

``AI_EMBED_PROVIDER`` selects the embedder and genuinely takes effect on the
persistence path (``ensure_embedding`` -> ``embed_text``):
    bow — default; hashed bag-of-words, 128-dim, deterministic, no network.
    hf  — ``sentence-transformers/all-MiniLM-L6-v2`` (384-dim), lazy-loaded,
           falling back to the bag-of-words embedder when the model or the
           dependency is unavailable.

Rows written by a different embedder are recomputed rather than compared, since
cosine similarity across different embedding spaces is not meaningful.
"""

import hashlib
import json
import math
import re

from sqlalchemy.orm import Session

from app.models import Ticket, TicketEmbedding

EMBED_DIM = 128
# Pinned hash algorithm for the BoW embedder. MD5 is cryptographically broken
# (SonarQube: "Use of a broken or weak cryptographic algorithm") — the test
# `test_embed_uses_sha256_not_md5` asserts on this constant, so a silent
# downgrade to MD5 fails loudly instead of passing unnoticed.
HASH_ALGORITHM = "sha256"
SIMILAR_STATUSES = ("resolved", "closed")
TOKEN_RE = re.compile(r"[a-z0-9]+")


def embed(text: str) -> list[float]:
    """Hashed bag-of-words using SHA-256, L2-normalized. Deterministic."""
    vector = [0.0] * EMBED_DIM
    for token in TOKEN_RE.findall(text.lower()):
        digest = hashlib.new(HASH_ALGORITHM, token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % EMBED_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [round(v / norm, 6) for v in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 0.0 if norm == 0 else round(dot / norm, 4)


_HF_MODEL = None


def _embed_hf(texts: list[str]) -> list[list[float]] | None:
    """Lazy-load MiniLM; return None when offline/missing dep (fallback BoW)."""
    global _HF_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        if _HF_MODEL is None:
            _HF_MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        return [list(map(float, v)) for v in _HF_MODEL.encode(texts, normalize_embeddings=True)]
    except Exception:
        return None


def embed_text(text: str) -> list[float]:
    from app.config import settings
    if settings.ai_embed_provider == "hf":
        out = _embed_hf([text])
        if out:
            return out[0]
    return embed(text)


def get_embed_dim() -> int:
    """Width of the vectors the active provider will produce.

    Returns the width of a real MiniLM vector when the HF embedder is loaded,
    otherwise the deterministic bag-of-words width.
    """
    if _HF_MODEL is not None:
        return len(_HF_MODEL.encode(["probe"], normalize_embeddings=True)[0])
    return EMBED_DIM


def _encode(vector: list[float]) -> str:
    return json.dumps(vector)


def _decode(raw: str) -> list[float]:
    return json.loads(raw)


def active_embedder_id() -> str:
    """Identifier for the embedder currently selected by ``AI_EMBED_PROVIDER``.

    Recorded next to every persisted vector so a later provider switch cannot
    be scored against vectors it did not produce.
    """
    from app.config import settings
    return "hf-minilm-l6-v2" if settings.ai_embed_provider == "hf" else "bow-sha256"


def ensure_embedding(db: Session, ticket: Ticket) -> list[float]:
    """Return the ticket vector, computing and persisting it if needed.

    The vector is produced by ``embed_text``, so ``AI_EMBED_PROVIDER`` actually
    takes effect on this path — not only on the ``embed_text`` call site. A row
    written by a different embedder (or of a different width) is recomputed
    rather than reused, because cosine similarity across incompatible
    embeddings is meaningless.
    """
    wanted_model = active_embedder_id()
    row = db.get(TicketEmbedding, ticket.id)
    if row is not None:
        stored = _decode(row.embedding)
        if row.model == wanted_model and len(stored) == row.dim:
            return stored
    vector = embed_text(f"{ticket.subject} {ticket.description}")
    if row is None:
        db.add(
            TicketEmbedding(
                ticket_id=ticket.id,
                embedding=_encode(vector),
                model=wanted_model,
                dim=len(vector),
            )
        )
    else:
        row.embedding = _encode(vector)
        row.model = wanted_model
        row.dim = len(vector)
    db.commit()
    return vector


def find_similar_tickets(
    db: Session, ticket: Ticket, limit: int = 3, min_similarity: float = 0.05
) -> list[dict]:
    """Most similar resolved/closed tickets (never the ticket itself)."""
    target = ensure_embedding(db, ticket)
    candidates = (
        db.query(Ticket)
        .filter(Ticket.id != ticket.id)
        .filter(Ticket.status.in_(SIMILAR_STATUSES))
        .all()
    )
    scored = [
        {
            "ticket_id": candidate.id,
            "subject": candidate.subject,
            "status": candidate.status,
            "similarity": cosine_similarity(target, ensure_embedding(db, candidate)),
        }
        for candidate in candidates
    ]
    scored = [s for s in scored if s["similarity"] >= min_similarity]
    scored.sort(key=lambda s: s["similarity"], reverse=True)
    return scored[:limit]
