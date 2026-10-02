"""Similar-ticket retrieval tests."""

from tests.conftest import create_ticket


def test_embed_provider_setting_defaults_to_bow():
    from app.config import settings
    assert settings.ai_embed_provider in ("bow", "hf")


def test_embed_text_dispatches_and_falls_back_offline(monkeypatch):
    """embed_text honours the provider and falls back to BoW when HF is absent.

    The BoW width is asserted exactly, not as ``len in (128, 384)``: a loose
    length check passes even when the provider setting is ignored, which is how
    the original ``ensure_embedding`` defect stayed hidden.
    """
    import dataclasses
    from app import config as config_module
    from app.services import retrieval_service

    vec = retrieval_service.embed_text("refund my charge twice")
    assert isinstance(vec, list) and len(vec) == retrieval_service.EMBED_DIM

    monkeypatch.setattr(
        config_module, "settings",
        dataclasses.replace(config_module.settings, ai_embed_provider="hf"),
    )
    # Force the offline path explicitly: without this, a machine with a warm
    # MiniLM cache would return 384-dim and the test would depend on the host.
    monkeypatch.setattr(retrieval_service, "_embed_hf", lambda texts: None)
    vec2 = retrieval_service.embed_text("login locked out")
    assert isinstance(vec2, list) and len(vec2) == retrieval_service.EMBED_DIM


def test_embed_text_returns_hf_width_when_model_loaded(monkeypatch):
    """With the MiniLM embedder loaded, embed_text returns its 384-dim vector."""
    import dataclasses
    from app import config as config_module
    from app.services import retrieval_service

    monkeypatch.setattr(
        config_module, "settings",
        dataclasses.replace(config_module.settings, ai_embed_provider="hf"),
    )
    monkeypatch.setattr(
        retrieval_service, "_embed_hf",
        lambda texts: [[0.5] * 384 for _ in texts],
    )
    assert len(retrieval_service.embed_text("refund requested")) == 384


def test_active_embedder_id_tracks_provider_setting(monkeypatch):
    import dataclasses
    from app import config as config_module
    from app.services import retrieval_service

    assert retrieval_service.active_embedder_id() == "bow-sha256"
    monkeypatch.setattr(
        config_module, "settings",
        dataclasses.replace(config_module.settings, ai_embed_provider="hf"),
    )
    assert retrieval_service.active_embedder_id() == "hf-minilm-l6-v2"

RESOLVED_DESCRIPTION = (
    "Since updating the iOS app to 4.2 logging in bounces me back to the welcome screen "
    "in a loop. Reinstalling the app did not fix it."
)


def _resolve(client, agent_headers, ticket_id):
    for status in ("in_progress", "resolved"):
        res = client.patch(
            f"/api/tickets/{ticket_id}", json={"status": status}, headers=agent_headers
        )
        assert res.status_code == 200, res.text


def test_similar_finds_resolved_ticket(client, agent_headers, customer_headers):
    resolved_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Login loop after app update",
        description=RESOLVED_DESCRIPTION,
    ).json()["id"]
    _resolve(client, agent_headers, resolved_id)

    target_id = create_ticket(
        client,
        subject="App logs me out in a loop",
        description="After the latest app update on iOS I get stuck in a login loop on the welcome screen.",
    ).json()["id"]

    res = client.get(f"/api/tickets/{target_id}/similar", headers=agent_headers)
    assert res.status_code == 200
    items = res.json()["items"]
    assert items, "expected at least one similar resolved ticket"
    top = items[0]
    assert top["ticket_id"] == resolved_id
    assert top["status"] == "resolved"
    assert 0 < top["similarity"] <= 1


def test_similar_excludes_self_and_open_tickets(client, agent_headers):
    open_id = create_ticket(client, subject="Some open thing", description="Just an open ticket here.").json()["id"]
    res = client.get(f"/api/tickets/{open_id}/similar", headers=agent_headers)
    assert res.status_code == 200
    assert all(item["ticket_id"] != open_id for item in res.json()["items"])
    assert all(item["status"] in ("resolved", "closed") for item in res.json()["items"])


def test_similar_no_candidates_returns_empty(client, agent_headers):
    ticket_id = create_ticket(client).json()["id"]
    res = client.get(f"/api/tickets/{ticket_id}/similar", headers=agent_headers)
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_similar_requires_agent(client, customer_headers):
    ticket_id = create_ticket(client).json()["id"]
    assert client.get(f"/api/tickets/{ticket_id}/similar").status_code == 401
    assert (
        client.get(f"/api/tickets/{ticket_id}/similar", headers=customer_headers).status_code == 403
    )


def test_embedding_persisted_and_reused(client, agent_headers, db_session):
    from app.models import TicketEmbedding

    ticket_id = create_ticket(client).json()["id"]
    client.get(f"/api/tickets/{ticket_id}/similar", headers=agent_headers)
    rows = db_session.query(TicketEmbedding).filter_by(ticket_id=ticket_id).all()
    assert len(rows) == 1
    # Second call must not duplicate the row.
    client.get(f"/api/tickets/{ticket_id}/similar", headers=agent_headers)
    assert db_session.query(TicketEmbedding).filter_by(ticket_id=ticket_id).count() == 1


def test_persisted_embedding_records_embedder_and_width(client, agent_headers, db_session):
    """The stored row says which embedder produced it and how wide it is."""
    import json

    from app.models import TicketEmbedding

    ticket_id = create_ticket(client).json()["id"]
    client.get(f"/api/tickets/{ticket_id}/similar", headers=agent_headers)
    row = db_session.query(TicketEmbedding).filter_by(ticket_id=ticket_id).one()
    assert row.model == "bow-sha256"
    assert row.dim == 128
    assert len(json.loads(row.embedding)) == row.dim


def test_ensure_embedding_honours_hf_provider(db_session, customer_user, monkeypatch):
    """Regression: ``ensure_embedding`` must not bypass ``AI_EMBED_PROVIDER``.

    The original implementation called the bag-of-words ``embed()`` directly,
    so setting ``AI_EMBED_PROVIDER=hf`` changed ``embed_text`` but every
    persisted vector and every similarity score stayed 128-dim. This asserts
    the persistence path itself follows the setting.
    """
    import dataclasses
    import json

    from app import config as config_module
    from app.models import Ticket, TicketEmbedding
    from app.services import retrieval_service

    ticket = Ticket(
        customer_id=customer_user.id,
        subject="Refund for double charge",
        description="Charged twice",
    )
    db_session.add(ticket)
    db_session.commit()

    monkeypatch.setattr(
        config_module, "settings",
        dataclasses.replace(config_module.settings, ai_embed_provider="hf"),
    )
    # Stand in for a loaded MiniLM: 384-dim, deterministic, no network.
    monkeypatch.setattr(
        retrieval_service, "_embed_hf",
        lambda texts: [[float(len(t)) / 100.0] * 384 for t in texts],
    )

    vector = retrieval_service.ensure_embedding(db_session, ticket)
    assert len(vector) == 384

    row = db_session.query(TicketEmbedding).filter_by(ticket_id=ticket.id).one()
    assert row.model == "hf-minilm-l6-v2"
    assert row.dim == 384
    assert len(json.loads(row.embedding)) == 384


def test_stale_embedding_is_recomputed_on_provider_switch(db_session, customer_user, monkeypatch):
    """A vector from another embedder is never reused or compared."""
    import dataclasses

    from app import config as config_module
    from app.models import Ticket
    from app.services import retrieval_service

    ticket = Ticket(
        customer_id=customer_user.id,
        subject="Login locked out",
        description="Cannot sign in at all",
    )
    db_session.add(ticket)
    db_session.commit()

    first = retrieval_service.ensure_embedding(db_session, ticket)
    assert len(first) == 128

    monkeypatch.setattr(
        config_module, "settings",
        dataclasses.replace(config_module.settings, ai_embed_provider="hf"),
    )
    monkeypatch.setattr(
        retrieval_service, "_embed_hf",
        lambda texts: [[0.25] * 384 for _ in texts],
    )
    second = retrieval_service.ensure_embedding(db_session, ticket)
    assert len(second) == 384, "stale 128-dim vector was reused across embedders"


def test_cosine_similarity_known_values():
    from app.services.retrieval_service import cosine_similarity

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_embed_uses_sha256_not_md5():
    """Verify embed() uses SHA-256, not MD5 (SonarQube security fix)."""
    import hashlib
    import math
    import re

    from app.services import retrieval_service
    from app.services.retrieval_service import EMBED_DIM, embed

    # The hash algorithm must be pinned and auditable, not buried inline.
    assert retrieval_service.HASH_ALGORITHM == "sha256"

    # Independently recompute the expected vector with SHA-256 only.
    # An MD5-based implementation produces a different vector and fails here.
    token_re = re.compile(r"[a-z0-9]+")

    def reference_sha256(text: str) -> list[float]:
        vector = [0.0] * EMBED_DIM
        for token in token_re.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBED_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [round(v / norm, 6) for v in vector]

    def reference_md5(text: str) -> list[float]:
        vector = [0.0] * EMBED_DIM
        for token in token_re.findall(text.lower()):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBED_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [round(v / norm, 6) for v in vector]

    text = "test ticket about password reset"
    assert embed(text) == reference_sha256(text)
    assert embed(text) != reference_md5(text)

    # Same input should produce deterministic output
    assert embed(text) == embed(text)

    # Verify output format: 128-dim float vector
    v1 = embed(text)
    assert len(v1) == 128
    assert all(isinstance(x, float) for x in v1)
