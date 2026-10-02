"""Tool definitions for the agent loop.

Every tool declares its parameters as a Pydantic model, so the JSON schema sent
to the model is derived from the same object that validates the arguments on
the way back. One source of truth means the advertised contract and the enforced
contract cannot drift apart.

Safety properties, because an agent can call these with arbitrary arguments:

* every tool is read-only — nothing here mutates a ticket, refunds money, or
  sends a message. The AI layer is suggestion-only by design (see
  `docs/spec.md`), and tools must not become a side door around that.
* arguments are validated before the handler runs, so a hallucinated or hostile
  argument fails loudly instead of reaching the database.
* every invocation is written to the audit log with its arguments and outcome.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ToolError(Exception):
    """A tool call could not be completed."""


class ToolResult(BaseModel):
    """Outcome of one tool invocation."""

    tool: str
    ok: bool
    data: Any = None
    error: str | None = None


class BaseTool:
    """A callable tool with a validated, self-describing argument schema."""

    name: str
    description: str
    args_model: type[BaseModel]
    # All tools are read-only. Kept explicit rather than assumed so adding a
    # mutating tool later requires flipping this and revisiting the audit rules.
    read_only: bool = True

    def run(self, arguments: dict[str, Any]) -> Any:
        """Validate ``arguments`` and execute the handler."""
        try:
            validated = self.args_model.model_validate(arguments or {})
        except ValidationError as exc:
            raise ToolError(f"invalid arguments for {self.name}: {exc.errors()}") from exc
        return self.handler(validated)

    @property
    def handler(self) -> Callable[[BaseModel], Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:
        """OpenAI-style function schema derived from the args model."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.args_model.model_json_schema(),
        }


# ------------------------------------------------------------ argument models


class _StrictArgs(BaseModel):
    """Base for tool argument models: unknown parameters are an error.

    A model can invent parameters. Ignoring them silently would hide a
    misreading of the tool contract, so `extra="forbid"` turns it into an
    explicit failure the agent loop can see and retry.
    """

    model_config = ConfigDict(extra="forbid")


class NoArgs(_StrictArgs):
    """No arguments."""


class TicketIdArgs(_StrictArgs):
    ticket_id: int = Field(..., gt=0, description="Numeric ticket id")


class TicketSearchArgs(_StrictArgs):
    query: str = Field(..., min_length=2, max_length=500)
    limit: int = Field(5, ge=1, le=25)


class PolicyLookupArgs(_StrictArgs):
    topic: str = Field(..., min_length=2, max_length=200)