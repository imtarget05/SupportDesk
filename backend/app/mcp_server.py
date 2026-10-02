"""MCP server exposing the support tools over the Model Context Protocol.

The same read-only tools the in-process agent calls are published over MCP, so
another agent or client can reuse this deployment's knowledge base and ticket
lookups without importing this codebase. The tool implementations are shared
rather than reimplemented: `build_registry` is the single source of truth, and
this module only adapts it to the MCP wire format.

Runs over stdio by default:

    python -m app.mcp_server

Transport is stdio, not HTTP, so no port is opened and no network listener is
created. Started with ``python -m``, not on import.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.tools import ToolRegistry
from app.tools.registry import build_registry

SERVER_NAME = "supportdesk"
SERVER_INSTRUCTIONS = (
    "Read-only access to the SupportDesk ticket system: knowledge-base search, "
    "ticket history, similar resolved tickets, and reference lookup. None of "
    "these tools can modify a ticket or send a message."
)


def build_server(db: Session) -> Any:
    """Create an MCPServer whose tools delegate to the shared registry."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name=SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        version="1.0.0",
    )
    registry = build_registry(db)

    @server.tool(
        name="search_knowledge_base",
        description=(
            "Search the support knowledge base (policies, FAQs, troubleshooting)."
        ),
    )
    def search_knowledge_base(query: str, limit: int = 5) -> str:
        return _as_text(registry.call("search_knowledge_base", {"query": query, "limit": limit}))

    @server.tool(
        name="get_ticket_history",
        description="Get the message thread and status for a ticket.",
    )
    def get_ticket_history(ticket_id: int) -> str:
        return _as_text(registry.call("get_ticket_history", {"ticket_id": ticket_id}))

    @server.tool(
        name="search_similar_tickets",
        description="Find previously resolved tickets similar to a query.",
    )
    def search_similar_tickets(query: str, limit: int = 5) -> str:
        return _as_text(
            registry.call("search_similar_tickets", {"query": query, "limit": limit})
        )

    @server.tool(
        name="lookup_order",
        description="Look up the reference associated with a ticket (demo data).",
    )
    def lookup_order(ticket_id: int) -> str:
        return _as_text(registry.call("lookup_order", {"ticket_id": ticket_id}))

    # Exposed so tests can assert the advertised contract without a subprocess.
    server.supportdesk_registry = registry  # type: ignore[attr-defined]
    return server


def _as_text(result: Any) -> str:
    """Serialize a ToolResult for the MCP text channel.

    A failed tool call is returned as text describing the failure rather than
    raised, so the calling agent can reason about it and try something else
    instead of seeing a transport-level error.
    """
    payload = {
        "ok": result.ok,
        "data": result.data,
        "error": result.error,
    }
    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return json.dumps({"ok": result.ok, "error": "result is not serializable"})


async def serve_stdio() -> None:
    """Run the MCP server over stdio."""
    db = SessionLocal()
    try:
        await build_server(db).run_stdio_async()
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover — process entry point
    import anyio

    anyio.run(serve_stdio)