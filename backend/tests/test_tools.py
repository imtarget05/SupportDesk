"""Agent tool registry: allowlist, argument validation, and audit trail."""

import pytest
from pydantic import Field

from app.tools import ToolRegistry, build_registry
from app.tools.base import BaseTool, TicketSearchArgs, ToolError
from tests.conftest import create_ticket


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo the text back."
    args_model = TicketSearchArgs

    @property
    def handler(self):
        return lambda args: {"query": args.query}


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


# ------------------------------------------------------------------- base tool


def test_tool_validates_arguments():
    assert EchoTool().run({"query": "hello there"}) == {"query": "hello there"}


def test_tool_rejects_invalid_arguments():
    with pytest.raises(ToolError, match="invalid arguments"):
        EchoTool().run({"query": "x"})  # below min_length


def test_tool_rejects_missing_arguments():
    with pytest.raises(ToolError):
        EchoTool().run({})


def test_tool_rejects_unexpected_arguments():
    """Strict models stop a hallucinated parameter from being silently ignored."""
    with pytest.raises(ToolError):
        EchoTool().run({"query": "hello", "sql": "DROP TABLE tickets"})


def test_tool_schema_is_derived_from_the_args_model():
    schema = EchoTool().json_schema()
    assert schema["name"] == "echo"
    assert "query" in schema["parameters"]["properties"]
    assert set(schema["parameters"]["required"]) == {"query"}


def test_tools_are_read_only_by_default():
    assert EchoTool().read_only is True


# -------------------------------------------------------------------- registry


def test_empty_registry_has_no_tools():
    assert ToolRegistry().names() == []


def test_duplicate_registration_is_rejected():
    registry = _registry()
    with pytest.raises(ToolError, match="already registered"):
        registry.register(EchoTool())


def test_unknown_tool_is_refused():
    result = _registry().call("drop_everything", {})
    assert result.ok is False
    assert "unknown tool" in result.error


def test_successful_call_is_audited():
    registry = _registry()
    result = registry.call("echo", {"query": "hi there"})
    assert result.ok is True
    assert result.data == {"query": "hi there"}
    assert registry.audit_log == [
        {"tool": "echo", "arguments": {"query": "hi there"}, "ok": True, "error": None}
    ]


def test_failed_call_is_audited():
    registry = _registry()
    registry.call("echo", {"query": "x"})
    entry = registry.audit_log[-1]
    assert entry["ok"] is False
    assert "invalid arguments" in entry["error"]


def test_schemas_expose_every_registered_tool():
    assert len(_registry().list_schemas()) == 1


# --------------------------------------------------------------- real registry


def test_build_registry_exposes_the_read_only_tools(db_session):
    registry = build_registry(db_session)
    assert set(registry.names()) == {
        "get_ticket_history",
        "search_similar_tickets",
        "search_knowledge_base",
        "lookup_order",
    }


def test_build_registry_tools_are_all_read_only(db_session):
    for name in build_registry(db_session).names():
        assert build_registry(db_session).get(name).read_only is True


def test_ticket_history_tool_returns_the_thread(db_session, client, customer_headers):
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Cannot log in",
        description="The reset link never arrives in my inbox.",
    ).json()["id"]

    result = build_registry(db_session).call("get_ticket_history", {"ticket_id": ticket_id})
    assert result.ok is True
    assert result.data["ticket_id"] == ticket_id
    assert result.data["subject"] == "Cannot log in"


def test_ticket_history_tool_on_missing_ticket(db_session):
    result = build_registry(db_session).call("get_ticket_history", {"ticket_id": 999999})
    assert result.ok is False
    assert "not found" in result.error


def test_ticket_history_tool_rejects_non_positive_id(db_session):
    result = build_registry(db_session).call("get_ticket_history", {"ticket_id": -1})
    assert result.ok is False
    assert "invalid arguments" in result.error


def test_order_lookup_tool_is_labelled_as_demo_data(db_session, client, customer_headers):
    ticket_id = create_ticket(
        client, headers=customer_headers, subject="Order question", description="Where is my order?"
    ).json()["id"]

    result = build_registry(db_session).call("lookup_order", {"ticket_id": ticket_id})
    assert result.ok is True
    assert result.data["reference"] == f"TKT-{ticket_id}"
    assert "demo data source" in result.data["note"]


def test_knowledge_search_tool_returns_evidence(db_session):
    result = build_registry(db_session).call(
        "search_knowledge_base", {"query": "refund policy", "limit": 3}
    )
    assert result.ok is True
    assert isinstance(result.data["evidence"], list)


def test_search_tool_limit_is_capped(db_session):
    """A model asking for 10,000 results must not be obeyed."""
    result = build_registry(db_session).call(
        "search_knowledge_base", {"query": "refund", "limit": 10_000}
    )
    assert result.ok is False
    assert "invalid arguments" in result.error