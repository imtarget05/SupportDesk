"""The stage functions a ticket workflow runs, in one place.

Both runtimes — Temporal and the in-process local runner — execute these exact
functions. The workflow bodies only decide *order and waiting*; what a stage
actually does lives here, so the two runtimes cannot drift apart.

Each stage is plain and synchronous, which is deliberate: it makes the same
function usable as a Temporal activity, as a local call, and as a test double.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.workflows.definitions import (
    activity_classify,
    activity_draft,
    activity_retrieve,
)

logger = logging.getLogger(__name__)

# How many times a stage is retried before the workflow gives up. Mirrors
# Temporal's own default so local runs surface failures at a similar rate.
MAX_STAGE_ATTEMPTS = 3
STAGE_BACKOFF_SECONDS = 0.2


@dataclass
class Pipeline:
    """The three AI stages, injectable so a test can substitute any of them."""

    classify: Callable[[dict[str, Any]], dict[str, Any]] = activity_classify
    retrieve: Callable[[dict[str, Any]], dict[str, Any]] = activity_retrieve
    draft: Callable[[dict[str, Any]], dict[str, Any]] = activity_draft


class StageError(Exception):
    """A stage failed after exhausting its retries."""


def run_stage(
    stage: Callable[[dict[str, Any]], dict[str, Any]],
    payload: dict[str, Any],
    *,
    attempts: int = MAX_STAGE_ATTEMPTS,
) -> dict[str, Any]:
    """Run one stage with bounded retries.

    Temporal retries activities itself, but the local runner has to do it here.
    Keeping the same ceiling means a failure is reported at a comparable point
    in both runtimes, which matters when comparing logs.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return stage(payload)
        except Exception as exc:  # noqa: BLE001 — retried, then re-raised below
            last_error = exc
            logger.warning(
                "Stage %s failed (attempt %d/%d): %s",
                getattr(stage, "__name__", stage),
                attempt + 1,
                attempts,
                exc,
            )
            if attempt + 1 < attempts:
                time.sleep(STAGE_BACKOFF_SECONDS)
    raise StageError(
        f"{getattr(stage, '__name__', 'stage')} failed after {attempts} attempts: {last_error}"
    ) from last_error