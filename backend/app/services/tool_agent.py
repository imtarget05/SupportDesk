"""Tool-calling agent loop.

A single LLM call cannot answer a support question that needs a fact the model
does not have ("what does our return policy say?"). The agent loop lets the
model request tools, feeds the results back, and repeats until it produces a
draft or hits the step ceiling.

The loop is where production systems actually break, so it is built around three
observed failure modes:

* **runaway loops.** A model can ask for the same tool with the same arguments
  forever. Identical consecutive calls are detected and the run stops, and a
  hard `TOOL_MAX_STEPS` ceiling bounds the rest.
* **tool errors becoming answer text.** A failed tool call is reported back to
  the model as an error string. If the model then writes that error into the
  draft, the guardrail rejects it, so a database outage cannot leak an internal
  message into a customer-facing reply.
* **unbounded token spend.** Every model turn is traced, so a runaway loop shows
  up as an obviously abnormal number of calls and cost per ticket.

Offline by default: with the stub provider the planner is deterministic keyword
dispatch, so the whole loop is testable with no API key. Real providers supply
their own planner via :class:`ToolPlanner`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import settings
from app.services import ai_service, guardrails
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """One requested tool invocation."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentStep:
    """One turn of the loop, kept for inspection and the audit trail."""

    step: int
    tool_calls: list[ToolCall]
    results: list[dict[str, Any]]
    draft: str


@dataclass
class AgentResult:
    """Outcome of an agent run."""

    draft: str
    steps: list[AgentStep] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    stopped_reason: str = "drafted"
    error: str | None = None


class ToolPlanner(Protocol):
    """Decides which tools to call next, given the ticket and prior results."""

    def next_calls(
        self, subject: str, description: str, transcript: list[str]
    ) -> list[ToolCall]: ...


class KeywordPlanner:
    """Deterministic offline planner.

    Used with the stub provider so the loop is exercisable offline. It picks a
    tool by scanning for the cue words that tool exists to answer, then returns
    nothing on the next turn so the model can draft.
    """

    CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "search_knowledge_base",
            ("policy", "policies", "faq", "how long", "window", "allowed"),
        ),
        ("search_similar_tickets", ("similar", "same issue", "previous")),
        ("get_ticket_history", ("already told", "history", "previous message")),
    )

    def next_calls(
        self, subject: str, description: str, transcript: list[str]
    ) -> list[ToolCall]:
        if transcript:
            # Facts have been gathered; let the model write.
            return []
        text = f"{subject} {description}".lower()
        calls: list[ToolCall] = []
        for name, cues in self.CUES:
            if any(cue in text for cue in cues):
                calls.append(ToolCall(name=name, arguments={"query": text[:200]}))
        return calls


class ToolCallingAgent:
    """Bounded loop: the planner requests tools, tools answer, the model drafts."""

    def __init__(
        self,
        registry: ToolRegistry,
        planner: ToolPlanner | None = None,
        max_steps: int | None = None,
    ) -> None:
        self.registry = registry
        self.planner = planner or KeywordPlanner()
        self.max_steps = max_steps or settings.tool_max_steps

    def run(self, subject: str, description: str, thread: str = "") -> AgentResult:
        """Run the loop and return a guardrail-validated draft.

        A run that trips a guardrail returns an error rather than the draft, so
        a caller can never mistake a blocked draft for a usable one.
        """
        transcript: list[str] = []
        steps: list[AgentStep] = []
        audit: list[dict[str, Any]] = []
        previous_signature: str | None = None
        stopped_reason = "drafted"

        for index in range(self.max_steps):
            try:
                calls = self.planner.next_calls(subject, description, transcript)
            except Exception as exc:  # noqa: BLE001 — a planner bug must not 500
                return AgentResult(
                    draft="",
                    steps=steps,
                    tool_calls=audit,
                    stopped_reason="planner_error",
                    error=f"{type(exc).__name__}: {exc}",
                )

            results = [self._invoke(call) for call in calls]
            audit.extend(
                {"name": call.name, "arguments": call.arguments, **result}
                for call, result in zip(calls, results)
            )

            signature = repr([(c.name, sorted(c.arguments.items())) for c in calls])
            if signature == previous_signature:
                # The planner is repeating itself; stop before burning tokens.
                stopped_reason = "repeated_tool_call"
                break
            previous_signature = signature

            transcript.extend(_render_transcript(calls, results))
            prompt = "\n".join(filter(None, [thread, *transcript])).strip()

            try:
                draft = ai_service.suggest_response(subject, description, prompt)
            except ai_service.AIProviderError as exc:
                return AgentResult(
                    draft="",
                    steps=steps,
                    tool_calls=audit,
                    stopped_reason="provider_error",
                    error=str(exc),
                )

            steps.append(
                AgentStep(step=index, tool_calls=calls, results=results, draft=draft)
            )

            if not calls:
                stopped_reason = "drafted"
                break
        else:
            stopped_reason = "max_steps"

        if not steps:
            return AgentResult(
                draft="",
                steps=steps,
                tool_calls=audit,
                stopped_reason=stopped_reason,
                error=f"agent produced no draft ({stopped_reason})",
            )

        # suggest_response already guardrail-checked the draft; this second pass
        # covers the neutral fallback draft that mode can return.
        try:
            guardrails.assert_safe_draft(steps[-1].draft, thread)
        except guardrails.GuardrailError as exc:
            return AgentResult(
                draft="",
                steps=steps,
                tool_calls=audit,
                stopped_reason="blocked_by_guardrail",
                error=str(exc),
            )

        return AgentResult(
            draft=steps[-1].draft,
            steps=steps,
            tool_calls=audit,
            stopped_reason=stopped_reason,
        )

    def _invoke(self, call: ToolCall) -> dict[str, Any]:
        result = self.registry.call(call.name, call.arguments)
        return {"ok": result.ok, "data": result.data, "error": result.error}


def _render_transcript(calls: list[ToolCall], results: list[dict[str, Any]]) -> list[str]:
    """Render tool results as text for the next model turn."""
    lines: list[str] = []
    for call, result in zip(calls, results):
        if result["ok"]:
            lines.append(
                f'<tool_result tool="{call.name}">{result["data"]}</tool_result>'
            )
        else:
            # The model is told the tool failed, in terms it can reason about.
            lines.append(
                f'<tool_result tool="{call.name}" error="true">{result["error"]}</tool_result>'
            )
    return lines