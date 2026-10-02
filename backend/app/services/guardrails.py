"""Deterministic safety guardrails applied on top of LLM output.

These are post-hoc, provider-agnostic checks. They never silently rewrite the
LLM's own words for the agent (that would pretend the model agreed); instead
they raise ``GuardrailError`` so the API layer can return 502 (default) or a
neutral fallback draft (``AI_GUARDRAIL_MODE=fallback``) and keep the ticket
untouched — the same contract as malformed JSON (spec scenario 2).

Layer model (defense-in-depth, ``backend/app/services/ai_service.py``):
    1. hardened system prompt + delimiter tags around untrusted ticket text
    2. this module: deterministic post-hoc checks on the LLM output
"""

from __future__ import annotations

import re


class GuardrailError(Exception):
    """LLM output violates a guardrail (unsafe to present to the agent)."""


# Security note: never let an LLM state these on its own authority. A support
# agent may, after verifying, but the AI layer is suggestion-only and must not
# author binding commitments or fabricate account/order actions.
COMMITMENT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"full\s+refund", re.I),
    re.compile(r"\bimmediate(?:ly)?\s+refund", re.I),
    re.compile(r"\bprocess(?:ed)?\s+(?:a\s+)?(?:full\s+)?refund", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:refund|compensati)", re.I),
    re.compile(r"\bcompensat(?:ion|e)\b", re.I),
    re.compile(r"\bguarantee(?:d|s)?\b", re.I),
    re.compile(r"\bI\s+have\s+(?:processed|issued|approved|authorized)\b", re.I),
]

# Claims that "ground" on a fact which may not exist in the thread. Allowed only
# when the same fact actually appears in the conversation (checked by
# ``assert_safe_draft`` against the thread text).
GROUNDING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b\d+\s*-?\s*day\s+(?:warranty|policy|return)", re.I),
    re.compile(r"\b(?:as\s+)?per\s+our\s+(?:FAQ|policy|terms)", re.I),
    re.compile(r"\bI\s*'?\s*ve\s+located\s+your\s+order", re.I),
    re.compile(r"\border\s*#\s*\d{3,}", re.I),
]

# Clear prompt-injection markers. If echoed into an AI summary/classification
# (or found in category/priority) the result is treated as steered and refused.
INJECTION_MARKERS: list[re.Pattern[str]] = [
    # Allow filler between the words: real attempts are phrased "ignore all
    # previous instructions", which the tighter pattern missed.
    re.compile(
        r"(?:ignore|disregard|forget)\b[^.!?\n]{0,40}?\b(?:instructions?|prompts?|rules?|directions?)",
        re.I,
    ),
    re.compile(r"reveal(?:ing)?\s+(?:your\s+)?system\s+prompt", re.I),
    re.compile(r"system\s+override", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an|the|in)", re.I),
    re.compile(r"pretend\s+(?:you\s+are|to\s+be)", re.I),
    re.compile(r"\byou\s+are\s+in\s+developer\s+mode\b", re.I),
]

# Internal details that must never reach a customer. A tool failure is reported
# back to the model as text, and a model that echoes it into its draft would
# otherwise turn "no such table: tickets" into a customer-facing reply. These
# patterns catch infrastructure and error text rather than promises, which
# COMMITMENT_PATTERNS above already covers.
INTERNAL_DETAIL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:operationalerror|integrityerror|sqlalchemy|traceback)\b", re.I),
    re.compile(r"\bno such table\b", re.I),
    re.compile(r"\b(?:internal\s+)?(?:server|stack)\s+trace\b", re.I),
    re.compile(r"\b(?:sqlite|postgresql|psycopg2|sqlalchemy)\.\w+\b", re.I),
]


# Neutral draft returned in ``fallback`` mode when the guardrail fires.
SAFE_FALLBACK_DRAFT = (
    "Thanks for reaching out. To make sure we give you an accurate answer, "
    "could you confirm the specific details of the issue? "
    "[Suggested draft — please review and edit before sending.]"
)


def flagged_patterns(text: str, patterns: list[re.Pattern[str]]) -> list[str]:
    """Return the pattern strings that match ``text`` (lowercased for match)."""
    lowered = text.lower()
    return [p.pattern for p in patterns if p.search(lowered)]


def has_injection_markers(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_MARKERS)


def assert_safe_draft(draft: str, thread: str = "") -> None:
    """Raise when a draft commits the AI to something or fabricates grounding.

    ``thread`` is lowercased internally so that grounding claims already backed
    by real conversation text are allowed.
    """
    hits = flagged_patterns(draft, COMMITMENT_PATTERNS)
    if hits:
        raise GuardrailError(
            "guardrail: draft contains disallowed commitment "
            f"({'; '.join(hits)})"
        )

    leaked = flagged_patterns(draft, INTERNAL_DETAIL_PATTERNS)
    if leaked:
        raise GuardrailError(
            f"guardrail: draft leaks internal detail ({'; '.join(leaked)})"
        )

    thread_lower = thread.lower()
    for pattern in GROUNDING_PATTERNS:
        if pattern.search(draft) and not pattern.search(thread_lower):
            raise GuardrailError(
                f"guardrail: draft references a fact not present in the thread "
                f"({pattern.pattern})"
            )


def assert_sterile_triage(result) -> None:
    """Reject a classification result that looks steered by the ticket text.

    Applied to the concatenated subject+description and to the AI summary.
    Doesn't mutate anything — raises so the API returns 502/keeps ticket.
    """
    if has_injection_markers(result.summary):
        raise GuardrailError(
            "guardrail: AI summary echoes prompt-injection markers; classification refused"
        )