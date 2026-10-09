"""Startup behavior: env validation and health endpoint."""

import importlib
import os

import pytest

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_health_endpoint_returns_ok(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_missing_jwt_secret_raises_at_import(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "x" * 31)  # Explicitly short (31 chars < 32-min)
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        importlib.reload(importlib.import_module("app.config"))


def test_invalid_database_url_raises_at_import(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "not-a-real-dialect://")
    with pytest.raises(RuntimeError, match="Unsupported database dialect"):
        importlib.reload(importlib.import_module("app.config"))


def test_dockerfile_exists():
    import pathlib
    assert pathlib.Path(__file__).resolve().parent.parent.parent / "Dockerfile"


def test_app_title_and_version(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["title"] == "SupportDesk API"


def test_render_cors_origin_is_the_canonical_pages_origin():
    from pathlib import Path

    blueprint = Path(__file__).resolve().parents[2] / "render.yaml"
    content = blueprint.read_text(encoding="utf-8")

    assert "value: https://supportdesk-cta.pages.dev" in content
    assert "value: https://supportdesk-aht.pages.dev" not in content


def test_pages_deploy_and_smoke_target_the_canonical_project():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    smoke = (root / "scripts/smoke-production.sh").read_text(encoding="utf-8")

    assert "|| 'supportdesk'" in workflow
    assert "${1:-https://supportdesk-cta.pages.dev}" in smoke
