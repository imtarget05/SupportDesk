from typing import Any

from pydantic import BaseModel, Field


class AISuggestionOut(BaseModel):
    response: str
    based_on_similar: list[int]


class ToolCallOut(BaseModel):
    """One tool invocation the agent made, with its outcome."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    data: Any = None
    error: str | None = None


class AgentRunOut(BaseModel):
    """Result of a tool-calling agent run.

    `stopped_reason` and `tool_calls` are returned so an operator can see not
    only what the agent drafted but why it stopped and what it looked at.
    """

    draft: str
    stopped_reason: str
    steps: int
    tool_calls: list[ToolCallOut] = Field(default_factory=list)


class WorkflowRunOut(BaseModel):
    """State of a durable workflow run.

    `events` is the ordered stage log, so an operator can see where a run
    stopped without reading Temporal history.
    """

    workflow_id: str
    ticket_id: int
    category: str
    priority: str
    summary: str
    confidence: float
    draft: str
    requires_approval: bool
    approved: bool
    sent: bool
    review_reason: str = ""
    events: list[str] = Field(default_factory=list)
    error: str | None = None


class SimilarTicketOut(BaseModel):
    ticket_id: int
    subject: str
    status: str
    similarity: float


class SimilarTicketsOut(BaseModel):
    items: list[SimilarTicketOut]
