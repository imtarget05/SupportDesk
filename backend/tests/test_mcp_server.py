"""MCP server: the shared tool registry published over Model Context Protocol.

Asserts the MCP contract (tool discovery and invocation) without spawning a
subprocess, so the suite stays fast and offline. Transport-level behaviour is
covered by running `python -m app.mcp_server` manually.

Async MCP calls are driven with `asyncio.run` rather than an async test plugin:
the MCP server API is coroutine-based, but adding `pytest-asyncio` purely for
eight tests is not worth another dependency in the environment.
"""

import asyncio
import json

import pytest

from app.mcp_server import SERVER_NAME, build_server
from app.tools.registry import build_registry
from tests.conftest import create_ticket


def _run(coro):
    return asyncio.run(coro)


def _text_of(result) -> str:
    """Pull the text payload out of an MCP call result."""
    return result.content[0].text


def test_server_advertises_the_shared_tools(db_session):
    tools = _run(build_server(db_session).list_tools())
    assert {tool.name for tool in tools} == {
        "search_knowledge_base",
        "get_ticket_history",
        "search_similar_tickets",
        "lookup_order",
    }


def test_advertised_tools_match_the_in_process_registry(db_session):
    """MCP must not drift from the tools the in-process agent uses."""
    mcp_names = {t.name for t in _run(build_server(db_session).list_tools())}
    assert mcp_names == set(build_registry(db_session).names())


def test_server_declares_itself_read_only(db_session):
    server = build_server(db_session)
    assert "read-only" in (server.instructions or "").lower()
    assert SERVER_NAME == "supportdesk"


def test_knowledge_search_returns_json(db_session):
    result = _run(
        build_server(db_session).call_tool("search_knowledge_base", {"query": "refund policy"})
    )
    payload = json.loads(_text_of(result))
    assert payload["ok"] is True
    assert "evidence" in payload["data"]


def test_ticket_history_returns_the_thread(db_session, client, customer_headers):
    ticket_id = create_ticket(
        client,
        headers=customer_headers,
        subject="Cannot log in",
        description="The password reset link never arrives.",
    ).json()["id"]

    result = _run(
        build_server(db_session).call_tool("get_ticket_history", {"ticket_id": ticket_id})
    )
    payload = json.loads(_text_of(result))
    assert payload["ok"] is True
    assert payload["data"]["ticket_id"] == ticket_id


def test_failed_tool_call_is_returned_as_text(db_session):
    """A failure is described to the caller, not raised as a transport error."""
    result = _run(build_server(db_session).call_tool("lookup_order", {"ticket_id": 999999}))
    payload = json.loads(_text_of(result))
    assert payload["ok"] is False
    assert "not found" in payload["error"]


def test_invalid_arguments_are_rejected_by_the_protocol(db_session):
    """The MCP layer validates arguments before the tool ever runs.

    A missing required parameter raises a protocol-level `ToolError` rather
    than reaching the handler, so an agent sees a precise correction to make.
    """
    from mcp.server.mcpserver.exceptions import ToolError as MCPProtocolToolError

    with pytest.raises(MCPProtocolToolError, match="query"):
        _run(build_server(db_session).call_tool("search_knowledge_base", {"limit": 9999}))


def test_out_of_range_argument_is_reported_inside_the_payload(db_session):
    """A well-formed but invalid value reaches the tool and is refused there.

    The MCP schema only declares types, so range limits are enforced by the
    tool's own argument model, and the failure is returned as text.
    """
    result = _run(
        build_server(db_session).call_tool(
            "search_knowledge_base", {"query": "refund", "limit": 9999}
        )
    )
    payload = json.loads(_text_of(result))
    assert payload["ok"] is False
    assert "invalid arguments" in payload["error"]


def test_unserializable_result_stays_valid_json(db_session):
    """A tool returning a non-serializable object must not break the server."""
    from app.mcp_server import _as_text

    class Exploding:
        ok = True
        data = object()
        error = None

    assert json.loads(_as_text(Exploding()))["ok"] is True


def test_every_tool_call_is_audited_in_the_shared_registry(db_session):
    server = build_server(db_session)
    _run(server.call_tool("search_knowledge_base", {"query": "refund"}))
    assert [entry["tool"] for entry in server.supportdesk_registry.audit_log] == [
        "search_knowledge_base"
    ]