"""Metrics endpoint tests.

SECURITY BEHAVIOUR CHANGE (explicit): this file previously asserted ``200`` for
an anonymous caller, which encoded the unauthenticated exposure being fixed.
It now asserts the secured behaviour instead: agent-only access, and presence
in the OpenAPI schema rather than being hidden from it.
"""

import pytest


def test_metrics_requires_auth(client):
    assert client.get("/api/metrics").status_code == 401


def test_metrics_rejects_authenticated_non_agent(client, customer_headers):
    assert client.get("/api/metrics", headers=customer_headers).status_code == 403


def test_metrics_endpoint(client, agent_headers):
    r = client.get("/api/metrics", headers=agent_headers)
    assert r.status_code == 200
    body = r.json()
    assert {"ai_calls", "ai_errors", "p50_latency_ms"} <= set(body)


def test_metrics_is_documented_in_schema(client, agent_user):
    """The route must be visible in the API contract, not hidden from it."""
    paths = client.get("/openapi.json").json()["paths"]
    assert "get" in paths["/api/metrics"]