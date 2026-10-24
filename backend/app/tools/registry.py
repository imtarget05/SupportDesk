"""The concrete tools exposed to the agent.

All are read-only lookups. `OrderLookupTool` reads the ticket table as a stand-in
for an order system on purpose: this project has no payments integration, and a
tool that pretended otherwise would be dishonest about what it can do.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Ticket, User
from app.tools.base import (
    BaseTool,
    CustomerIdArgs,
    PolicyLookupArgs,
    ProposeEscalationArgs,
    ProposeExternalReplyArgs,
    ProposeRefundArgs,
    TicketIdArgs,
    TicketSearchArgs,
    ToolError,
    ToolResult,
)


def _ticket_or_error(db: Session, ticket_id: int) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise ToolError(f"ticket {ticket_id} not found")
    return ticket


class TicketHistoryTool(BaseTool):
    name = "get_ticket_history"
    description = (
        "Get the full message thread and lifecycle status for a ticket. "
        "Use this to see what the customer has already been told."
    )
    args_model = TicketIdArgs

    @property
    def handler(self):
        def run(args: TicketIdArgs) -> dict[str, Any]:
            ticket = _ticket_or_error(self.db, args.ticket_id)
            return {
                "ticket_id": ticket.id,
                "subject": ticket.subject,
                "status": ticket.status,
                "category": ticket.category,
                "priority": ticket.priority,
                "messages": [
                    {"sender": m.sender.role, "content": m.content} for m in ticket.messages
                ],
            }

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class SimilarTicketsTool(BaseTool):
    name = "search_similar_tickets"
    description = (
        "Find previously resolved tickets similar to a query, with similarity "
        "scores. Use this to reuse an answer that already worked."
    )
    args_model = TicketSearchArgs

    @property
    def handler(self):
        def run(args: TicketSearchArgs) -> dict[str, Any]:
            from app.services import retrieval_service

            # An unsaved Ticket acts as a query carrier: retrieval embeds it and
            # excludes it from its own candidate set because it has no id.
            probe = Ticket(subject=args.query, description="")
            matches = retrieval_service.find_similar_tickets(self.db, probe, limit=args.limit)
            return {"query": args.query, "matches": matches}

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class KnowledgeSearchTool(BaseTool):
    name = "search_knowledge_base"
    description = (
        "Search the support knowledge base (policies, FAQs, troubleshooting). "
        "Use this before promising anything about a policy or a return window."
    )
    args_model = TicketSearchArgs

    @property
    def handler(self):
        def run(args: TicketSearchArgs) -> dict[str, Any]:
            from app.services.knowledge_base import get_knowledge_base

            evidence = get_knowledge_base().retrieve(args.query, top_k=args.limit)
            return {
                "query": args.query,
                "evidence": [
                    {"source": e.source, "score": e.score, "content": e.content}
                    for e in evidence
                ],
            }

        return run


class OrderLookupTool(BaseTool):
    name = "lookup_order"
    description = (
        "Look up the reference associated with a ticket. This is a demonstration "
        "data source, not a real payments integration."
    )
    args_model = TicketIdArgs

    @property
    def handler(self):
        def run(args: TicketIdArgs) -> dict[str, Any]:
            ticket = _ticket_or_error(self.db, args.ticket_id)
            return {
                "reference": f"TKT-{ticket.id}",
                "state": ticket.status,
                "note": "demo data source; no order system is connected",
            }

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class CustomerHistoryTool(BaseTool):
    name = "get_customer_history"
    description = (
        "Get customer profile, past ticket count, resolution history, and SLA tier. "
        "Use this to understand user context and VIP status."
    )
    args_model = CustomerIdArgs

    @property
    def handler(self):
        def run(args: CustomerIdArgs) -> dict[str, Any]:
            user = self.db.get(User, args.customer_id)
            if user is None:
                raise ToolError(f"customer {args.customer_id} not found")
            tickets = (
                self.db.query(Ticket)
                .filter(Ticket.customer_id == args.customer_id)
                .all()
            )
            total = len(tickets)
            open_count = sum(
                1 for t in tickets if t.status in ("open", "in_progress", "pending")
            )
            resolved_count = sum(
                1 for t in tickets if t.status in ("resolved", "closed")
            )
            tier = "vip" if total >= 5 else "standard"
            return {
                "customer_id": user.id,
                "name": user.name,
                "email": user.email,
                "total_tickets": total,
                "open_tickets": open_count,
                "resolved_tickets": resolved_count,
                "sla_tier": tier,
                "recent_tickets": [
                    {
                        "id": t.id,
                        "subject": t.subject,
                        "status": t.status,
                        "priority": t.priority,
                    }
                    for t in tickets[-3:]
                ],
            }

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class ProposeRefundTool(BaseTool):
    name = "propose_refund"
    description = (
        "Propose a customer refund for human approval. Does NOT issue the refund directly. "
        "A human operator must approve this action."
    )
    args_model = ProposeRefundArgs
    read_only = False

    @property
    def handler(self):
        def run(args: ProposeRefundArgs) -> dict[str, Any]:
            ticket = _ticket_or_error(self.db, args.ticket_id)
            from app.services import hitl_service

            proposal = hitl_service.create_proposal(
                self.db,
                ticket_id=ticket.id,
                action_type="refund",
                payload={"amount": args.amount, "reason": args.reason},
                reasoning=args.reason,
                confidence=1.0,
                created_by="ai_agent",
            )
            return {
                "proposal_id": proposal.id,
                "action_type": "refund",
                "status": proposal.status,
                "amount": args.amount,
                "message": (
                    "Refund proposal created and queued for human approval. "
                    "No money was transferred."
                ),
            }

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class ProposeEscalationTool(BaseTool):
    name = "propose_escalation"
    description = (
        "Propose escalating a ticket to a higher tier (1-3) for human approval. "
        "Does NOT mutate ticket state directly."
    )
    args_model = ProposeEscalationArgs
    read_only = False

    @property
    def handler(self):
        def run(args: ProposeEscalationArgs) -> dict[str, Any]:
            ticket = _ticket_or_error(self.db, args.ticket_id)
            from app.services import hitl_service

            proposal = hitl_service.create_proposal(
                self.db,
                ticket_id=ticket.id,
                action_type="escalate",
                payload={
                    "escalation_level": args.escalation_level,
                    "reason": args.reason,
                },
                reasoning=args.reason,
                confidence=1.0,
                created_by="ai_agent",
            )
            return {
                "proposal_id": proposal.id,
                "action_type": "escalate",
                "status": proposal.status,
                "target_level": args.escalation_level,
                "message": (
                    "Escalation proposal created and queued for human approval. "
                    "Ticket was not modified."
                ),
            }

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class ProposeExternalReplyTool(BaseTool):
    name = "propose_external_reply"
    description = (
        "Propose an outbound customer message for human approval. "
        "Does NOT send the message directly."
    )
    args_model = ProposeExternalReplyArgs
    read_only = False

    @property
    def handler(self):
        def run(args: ProposeExternalReplyArgs) -> dict[str, Any]:
            ticket = _ticket_or_error(self.db, args.ticket_id)
            from app.services import hitl_service

            proposal = hitl_service.create_proposal(
                self.db,
                ticket_id=ticket.id,
                action_type="external_reply",
                payload={"content": args.content, "reason": args.reason},
                reasoning=args.reason,
                confidence=1.0,
                created_by="ai_agent",
            )
            return {
                "proposal_id": proposal.id,
                "action_type": "external_reply",
                "status": proposal.status,
                "message": (
                    "External reply proposal queued for human approval. "
                    "No message was sent."
                ),
            }

        return run

    def __init__(self, db: Session) -> None:
        self.db = db


class ToolRegistry:
    """Allowlist of callable tools, with an audit trail per invocation.

    The allowlist is the security boundary: an agent may only reach tools that
    were explicitly registered here, and every call is recorded so a reviewer
    can see exactly what the model asked for.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self.audit_log: list[dict[str, Any]] = []

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        return tool

    def names(self) -> list[str]:
        return list(self._tools)

    def list_schemas(self) -> list[dict[str, Any]]:
        return [tool.json_schema() for tool in self._tools.values()]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Invoke a tool by name, validating arguments and recording the call."""
        try:
            data = self.get(name).run(arguments or {})
        except ToolError as exc:
            self._record(name, arguments, ok=False, error=str(exc))
            return ToolResult(tool=name, ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — a bad tool must not crash the agent
            self._record(name, arguments, ok=False, error=type(exc).__name__)
            return ToolResult(tool=name, ok=False, error=f"{type(exc).__name__}: {exc}")
        self._record(name, arguments, ok=True)
        return ToolResult(tool=name, ok=True, data=data)

    def _record(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        ok: bool,
        error: str | None = None,
    ) -> None:
        self.audit_log.append(
            {"tool": name, "arguments": arguments or {}, "ok": ok, "error": error}
        )


def build_registry(
    db: Session,
    *,
    include_proposals: bool = False,
    include_customer_history: bool = False,
) -> ToolRegistry:
    """Registry with tools bound to this database session.

    By default, returns the 4 core read-only lookup tools required by MCP and
    standard contracts. Set include_proposals=True and include_customer_history=True
    (or use build_hitl_registry) to expose action proposals and extended history.
    """
    registry = ToolRegistry()
    registry.register(TicketHistoryTool(db))
    registry.register(SimilarTicketsTool(db))
    registry.register(KnowledgeSearchTool())
    registry.register(OrderLookupTool(db))
    if include_customer_history:
        registry.register(CustomerHistoryTool(db))
    if include_proposals:
        registry.register(ProposeRefundTool(db))
        registry.register(ProposeEscalationTool(db))
        registry.register(ProposeExternalReplyTool(db))
    return registry


def build_hitl_registry(db: Session) -> ToolRegistry:
    """Registry with all tools including read-only lookups and proposal tools."""
    return build_registry(
        db, include_proposals=True, include_customer_history=True
    )
