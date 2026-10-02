"""Per-model token pricing, used to turn usage into money.

Rates are USD per 1M tokens, as published by each provider, and are pinned with
an ``as_of`` date so a cost figure in a report can be reproduced: the number is
only meaningful together with the rate table version it was computed from.

An unknown model returns ``None`` rather than a guess. A missing cost is a
reportable gap; a fabricated cost is a lie in a metrics dashboard.
"""

from __future__ import annotations

# Bump when rates change; recorded alongside every computed cost.
PRICING_TABLE_VERSION = "2026-10-10"
PRICING_AS_OF = "2026-10-10"

# model -> (input USD / 1M tokens, output USD / 1M tokens)
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    # Anthropic
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # Cloudflare Workers AI
    "@cf/meta/llama-3.1-8b-instruct": (0.20, 0.20),
}

# Rough characters per token, used only when a provider reports no usage
# counter. Counts derived from it are always labelled as estimates.
CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    """Approximate token count for text, when the provider reports none."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _rate_for(model: str) -> tuple[float, float] | None:
    rate = MODEL_PRICES.get(model)
    if rate is not None:
        return rate
    # Longest-prefix match so a dated snapshot (gpt-4o-2024-08-06) prices
    # against its base model.
    for name, candidate in sorted(MODEL_PRICES.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(name):
            return candidate
    return None


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Cost in USD for one call, or ``None`` when the model is not priced."""
    rate = _rate_for(model)
    if rate is None:
        return None
    input_rate, output_rate = rate
    return round(
        (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000, 8
    )


def is_priced(model: str) -> bool:
    return _rate_for(model) is not None
