"""LangChain agent abstraction layer.

This module provides a unified interface for LLM operations using LangChain.
It wraps the existing AI providers (stub, OpenAI, Cloudflare) in LangChain's
interface, enabling the ticket pipeline to use them interchangeably.

The agent provides:
    - LLM invocation for classification and generation
    - Tool abstraction for external API calls
    - Chain composition for multi-step operations
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.config import settings
from app.services import ai_service

logger = logging.getLogger(__name__)


class LangChainAgent:
    """LangChain-based agent for support ticket operations.

    Wraps the existing AI providers in LangChain's interface for use
    in the ticket processing pipeline.
    """

    def __init__(self, provider: str | None = None):
        self._provider = provider or settings.ai_provider
        self._llm = None
        self._tools: dict[str, Callable] = {}

    def _get_llm(self):
        """Get or create the LangChain LLM instance."""
        if self._llm is not None:
            return self._llm

        try:
            from langchain_core.language_models import BaseLLM

            # Create a wrapper that adapts our existing providers to LangChain
            self._llm = _ExistingProviderLLM(self._provider)
            return self._llm

        except ImportError:
            logger.warning("LangChain not available; using direct provider calls")
            self._llm = None
            return None

    def register_tool(self, name: str, func: Callable) -> None:
        """Register a tool that the agent can call."""
        self._tools[name] = func

    def classify(self, subject: str, description: str) -> dict[str, Any]:
        """Classify a ticket using the LLM.

        Returns a dict with category, priority, summary, and confidence.
        """
        try:
            result = ai_service.analyze_ticket(subject, description)
            return {
                "category": result.category.value,
                "priority": result.priority.value,
                "summary": result.summary,
                "confidence": result.confidence,
            }
        except ai_service.AIProviderError as exc:
            logger.warning("Classification failed: %s", exc)
            raise

    def generate(
        self,
        prompt: str,
        context: str = "",
        max_tokens: int = 500,
    ) -> str:
        """Generate text using the LLM.

        Args:
            prompt: The prompt to send to the LLM.
            context: Additional context to include.
            max_tokens: Maximum tokens to generate.

        Returns:
            The generated text.
        """
        try:
            # Use the existing suggest_response for generation
            return ai_service.suggest_response(prompt, context, "")
        except ai_service.AIProviderError as exc:
            logger.warning("Generation failed: %s", exc)
            raise

    def draft_response(
        self,
        subject: str,
        description: str,
        thread: str,
        evidence: list[dict[str, Any]] | None = None,
    ) -> str:
        """Draft a response using the LLM with retrieved evidence.

        Args:
            subject: The ticket subject.
            description: The ticket description.
            thread: The conversation thread.
            evidence: Retrieved evidence from the knowledge base.

        Returns:
            The drafted response.
        """
        try:
            # Build context from evidence if provided
            evidence_text = ""
            if evidence:
                evidence_text = "\n\nRelevant knowledge base entries:\n"
                for i, ev in enumerate(evidence, 1):
                    evidence_text += f"\n[{i}] Source: {ev.get('source', 'unknown')}\n"
                    evidence_text += f"{ev.get('content', '')}\n"

            # Combine thread with evidence
            full_thread = thread + evidence_text if evidence_text else thread

            return ai_service.suggest_response(subject, description, full_thread)
        except ai_service.AIProviderError as exc:
            logger.warning("Draft generation failed: %s", exc)
            raise

    def get_stats(self) -> dict[str, Any]:
        """Return statistics about the agent."""
        return {
            "provider": self._provider,
            "tools_registered": list(self._tools.keys()),
            "langchain_available": self._get_llm() is not None,
        }


class _ExistingProviderLLM:
    """Adapter that wraps existing AI providers in LangChain's LLM interface.

    This allows the existing stub/OpenAI/Cloudflare providers to be used
    within LangChain's framework without duplicating provider logic.
    """

    def __init__(self, provider: str):
        self._provider = provider

    def invoke(self, prompt: str, **kwargs: Any) -> str:
        """Invoke the LLM with a prompt.

        The prompt is treated as the main content (ticket description) so it
        actually reaches the model rather than being interpreted as a bare
        subject line.
        """
        try:
            return ai_service.suggest_response("LLM prompt", prompt, "")
        except ai_service.AIProviderError as exc:
            raise RuntimeError(f"LLM invocation failed: {exc}") from exc

    def __call__(self, prompt: str, **kwargs: Any) -> str:
        """Make the LLM callable."""
        return self.invoke(prompt, **kwargs)


# Singleton instance
_agent: LangChainAgent | None = None


def get_agent() -> LangChainAgent:
    """Get or create the singleton LangChainAgent instance."""
    global _agent
    if _agent is None:
        _agent = LangChainAgent()
    return _agent


def reset_agent() -> None:
    """Reset the singleton (for testing)."""
    global _agent
    _agent = None