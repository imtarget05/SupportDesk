"""In-process fixed-window rate limiter for anonymous write endpoints.

Deliberately dependency-free: this project ships no Redis and this is a
single-instance guard against scripted abuse, not a distributed quota.

The bucket is keyed on ``request.client.host``, which is the peer address from
the ASGI scope. ``X-Forwarded-For`` is never read directly here: a
client-supplied header is trivially spoofed and would defeat the control.

Behind a TLS-terminating proxy that makes every caller share one bucket unless
uvicorn resolves the real address itself. Both ingresses in this project append
the address they observed (``$proxy_add_x_forwarded_for`` in nginx; Render's edge
behaves the same way), so the container is started with
``--proxy-headers --forwarded-allow-ips="*"``, which makes uvicorn take the
rightmost entry — the address the ingress actually saw, not one the client
chose. Spoofing requires reaching the backend without passing the ingress, which
is why docker-compose binds the backend port to loopback.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimitExceeded(Exception):
    """Raised when a key has exhausted its allowance for the current window."""

    def __init__(self, retry_after_s: int):
        self.retry_after_s = retry_after_s
        super().__init__(f"rate limit exceeded; retry in {retry_after_s}s")


_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)


def check(key: str, *, limit: int, window_s: float) -> None:
    """Record a hit for `key` or raise RateLimitExceeded.

    A `limit` of zero or less disables the limiter entirely, which keeps the
    control opt-out-able in configuration without a code change.
    """
    if limit <= 0:
        return

    now = time.monotonic()
    cutoff = now - window_s
    with _lock:
        bucket = _hits[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + window_s - now) + 1)
            raise RateLimitExceeded(retry_after)
        bucket.append(now)


def reset() -> None:
    """Drop all recorded hits. Used by tests to isolate global state."""
    with _lock:
        _hits.clear()