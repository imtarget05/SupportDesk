"""Monthly AI spend cap — a money-based hard stop, not a token guess.

The cap sums the ``cost_usd`` recorded on ``ai_call_traces`` for the current
calendar month and refuses the next paid provider call once that sum reaches
the configured budget (``AI_MONTHLY_BUDGET_USD``, default $10 per
``docs/DECISIONS.md`` D5/D8).

Why money and not tokens: token counts mean nothing across models, and the
pricing table already turns usage into the number an invoice actually shows.

Local providers (stub, distilbert) bill nothing and are never capped, so
development keeps working with the cap enabled. The cap is a *policy* object:
this module only reports spend and the configured limit — deciding to refuse a
call, and what error the caller sees, belongs to ``ai_service``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import AICallTrace

# Providers that bill per token. Local/offline providers are absent on purpose.
PAID_PROVIDERS = frozenset({"openai", "anthropic", "cloudflare"})


def is_paid_provider(provider: str) -> bool:
    """True when this provider sends a bill for its tokens."""
    return provider in PAID_PROVIDERS


def budget_cap_usd() -> float | None:
    """The configured monthly cap in USD, or ``None`` when the cap is off.

    A cap of 0 (or any negative value) disables enforcement entirely — an
    explicit opt-out, so a missing env var can never silently mean "unlimited":
    the default is the $10 approved budget.
    """
    try:
        cap = float(getattr(settings, "ai_monthly_budget_usd", 0.0))
    except (TypeError, ValueError):
        # An unreadable cap must not fail open: refuse to say "no limit".
        return 0.0
    return cap if cap > 0 else None


def _month_start(now: datetime | None = None) -> datetime:
    """First instant of the current calendar month, naive UTC.

    ``ai_call_traces.created_at`` is a naive ``DateTime`` (server default), so
    the comparison must be naive too — an aware datetime against a naive column
    raises on Postgres and silently misbehaves on SQLite.
    """
    now = (now or datetime.utcnow()).replace(tzinfo=None)
    return datetime(now.year, now.month, 1)


def monthly_spend_usd(session: Session | None = None, now: datetime | None = None) -> float:
    """Sum of priced call costs recorded since the start of the current month.

    Rows whose ``cost_usd`` is NULL (a model missing from the pricing table)
    are skipped: SQL ``SUM`` ignores NULL, and reporting them as $0 would claim
    "this call was free" when the truth is "we could not price it". Those calls
    are surfaced as ``unpriced_calls`` by ``tracing.summarize``.

    Raises whatever the database raises — the caller decides whether an
    unreadable spend fails open or closed (it must fail closed).
    """
    start = _month_start(now)
    if session is not None:
        return _sum_since(session, start)
    db = SessionLocal()
    try:
        return _sum_since(db, start)
    finally:
        db.close()


def _sum_since(session: Session, start: datetime) -> float:
    total = (
        session.query(func.coalesce(func.sum(AICallTrace.cost_usd), 0.0))
        .filter(AICallTrace.created_at >= start)
        .scalar()
    )
    return float(total or 0.0)


def budget_state(session: Session | None = None, now: datetime | None = None) -> dict:
    """Spend/cap snapshot for the metrics API.

    Returns a structured dict rather than a boolean so an operator can see how
    close the service is to the limit *before* calls start failing.
    """
    cap = budget_cap_usd()
    spend = monthly_spend_usd(session, now)
    remaining = None if cap is None else max(0.0, round(cap - spend, 6))
    return {
        "provider": str(getattr(settings, "ai_provider", "unknown")),
        "paid_provider": is_paid_provider(str(getattr(settings, "ai_provider", "unknown"))),
        "cap_usd": cap,
        "month_to_date_usd": round(spend, 6),
        "remaining_usd": remaining,
        "window_starts": _month_start(now).isoformat() + "Z",
        "period": "calendar-month-utc",
    }
