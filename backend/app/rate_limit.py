"""Rate limiter for anonymous write endpoints.

Two interchangeable backends implement the same policy — a per-key sliding
window with a fixed allowance:

  * **Redis** (``REDIS_URL`` / ``UPSTASH_REDIS_URL``) — one sorted set per key,
    scored in wall-clock seconds, so the allowance is shared by every instance
    and worker behind the ingress. This is the multi-instance case a
    process-local dict silently under-counts.
  * **In-process** (default) — the same window over a module-level ``deque``
    guarded by a ``threading.Lock``. No dependency, no second service.

Redis is opt-in and never load-bearing: when the URL is unset, the package is
not installed, or the server does not answer, the limiter falls back to the
in-process guard it has always used. A rate limiter that turns into a hard
dependency would add an outage mode to the endpoint it is meant to protect,
so unavailability here costs distributed counting, not availability.

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

Time source: the in-process backend scores hits with ``time.monotonic()``,
which is immune to clock steps, while the Redis backend uses ``time.time()``
because a monotonic reading means nothing on another machine (let alone after a
restart) and the counter has to be readable by all of them. Both windows are
relative, so the policy is unaffected — only the meaning of the stored score
changes.

Atomicity: recording a hit and reading the count back exercises one pipeline
round trip, so two concurrent requests can at worst widen a burst by a single
request instead of serialising it through a ``WATCH``/Lua script. The window
policy is otherwise identical between backends.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# Namespace for Redis keys, so a shared database cannot collide with other
# buckets (sessions, caches) held by the same server.
KEY_PREFIX = "ratelimit:"


class RateLimitExceeded(Exception):
    """Raised when a key has exhausted its allowance for the current window."""

    def __init__(self, retry_after_s: int):
        self.retry_after_s = retry_after_s
        super().__init__(f"rate limit exceeded; retry in {retry_after_s}s")


_lock = threading.Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)

# Cached Redis client, and the reason it is not currently usable. Both are
# cleared by reset() so a configuration change (or the next test) re-resolves
# the backend rather than inheriting a stale decision.
_client: Any | None = None
_client_unavailable = False


def _import_redis() -> Any | None:
    """Return the redis module, or None when it is not installed.

    Imported lazily so the limiter — and the whole app — keeps importing on a
    machine that has not installed the optional package.
    """
    try:
        import redis
    except ImportError:
        return None
    return redis


def _new_client(url: str) -> Any | None:
    """Open a Redis client for `url`, or None when it cannot be reached.

    Returns None rather than raising so callers only have to handle "no Redis"
    one way. The decision is logged once, at the point where the configuration
    stopped matching reality.
    """
    redis_module = _import_redis()
    if redis_module is None:
        logger.warning(
            "REDIS_URL is set but the 'redis' package is not installed; "
            "falling back to in-process rate limiting"
        )
        return None
    try:
        client = redis_module.Redis.from_url(
            url,
            socket_connect_timeout=settings.redis_connect_timeout_s,
            socket_timeout=settings.redis_connect_timeout_s,
            decode_responses=True,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure means "use fallback"
        logger.warning(
            "Redis rate-limit backend unavailable (%s: %s); "
            "falling back to in-process rate limiting",
            type(exc).__name__,
            exc,
        )
        return None
    return client


def _redis_client() -> Any | None:
    """Resolve the distributed backend, or None to use the in-process one.

    A negative answer is sticky: paying a connect timeout on every request to a
    Redis that is already known to be down is worse than dropping to the
    per-instance guard.
    """
    global _client, _client_unavailable
    if _client is not None:
        return _client
    if _client_unavailable:
        return None
    url = settings.redis_url
    if not url:
        _client_unavailable = True
        return None
    _client = _new_client(url)
    if _client is None:
        _client_unavailable = True
    return _client


def check(key: str, *, limit: int, window_s: float) -> None:
    """Record a hit for `key` or raise RateLimitExceeded.

    A `limit` of zero or less disables the limiter entirely, which keeps the
    control opt-out-able in configuration without a code change.
    """
    if limit <= 0:
        return

    client = _redis_client()
    if client is None:
        _check_in_process(key, limit=limit, window_s=window_s)
    else:
        _check_redis(client, key, limit=limit, window_s=window_s)


def _check_in_process(key: str, *, limit: int, window_s: float) -> None:
    """Sliding window over a local deque. Single instance, no service."""
    now = time.monotonic()
    cutoff = now - window_s
    with _lock:
        bucket = _hits[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = _retry_after(oldest=bucket[0], now=now, window_s=window_s)
            raise RateLimitExceeded(retry_after)
        bucket.append(now)


def _check_redis(client: Any, key: str, *, limit: int, window_s: float) -> None:
    """Sliding window over a Redis sorted set. Shared across instances."""
    redis_key = KEY_PREFIX + key
    now = time.time()
    cutoff = now - window_s
    # Unique member: score ties are fine for a set, but a member that repeats
    # would collide with itself and only count once.
    member = f"{now}:{uuid.uuid4().hex}"
    ttl = max(1, int(window_s) + 1)

    try:
        pipe = client.pipeline(transaction=False)
        pipe.zremrangebyscore(redis_key, "-inf", cutoff)  # drop the expired
        pipe.zadd(redis_key, {member: now})  # charge this request
        pipe.zcard(redis_key)
        pipe.zrange(redis_key, 0, 0, withscores=True)
        pipe.expire(redis_key, ttl)
        _expired, _added, count, oldest, _ttl = pipe.execute()
    except Exception as exc:  # noqa: BLE001 - outage must not fail the request
        logger.warning(
            "Redis rate-limit check failed for %r (%s: %s); "
            "using the in-process window for this call",
            key,
            type(exc).__name__,
            exc,
        )
        _check_in_process(key, limit=limit, window_s=window_s)
        return

    if count <= limit:
        return

    # Over budget: undo the charge, otherwise a rejected request would push the
    # oldest hit further out for everybody else sharing the key.
    try:
        client.zrem(redis_key, member)
    except Exception:  # noqa: BLE001 - best-effort, the window clears itself
        logger.debug("Redis rate-limit cleanup failed for %r", redis_key, exc_info=True)

    oldest_score = float(oldest[0][1]) if oldest else now
    raise RateLimitExceeded(
        _retry_after(oldest=oldest_score, now=now, window_s=window_s)
    )


def _retry_after(*, oldest: float, now: float, window_s: float) -> int:
    """Seconds until the oldest hit in the window frees a slot."""
    return max(1, int(oldest + window_s - now) + 1)


def reset() -> None:
    """Drop all recorded hits, and the cached backend choice.

    Used by tests to isolate global state; also how a redeployed ``REDIS_URL``
    takes effect without a restart of the worker process.
    """
    global _client, _client_unavailable
    with _lock:
        _hits.clear()
    _client = None
    _client_unavailable = False
