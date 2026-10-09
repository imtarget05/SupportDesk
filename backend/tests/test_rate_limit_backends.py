"""Rate-limit backends: Redis when configured, in-process when not.

Redis is the multi-instance answer — every worker draws on one shared
allowance — but a limiter must never be the reason a legitimate request fails,
so an unset URL, a missing package, an unreachable server, or an error
mid-check all degrade to the in-process window.

The Redis branch runs against fakeredis when it is installed (a test-only
dependency, listed in requirements.txt) and is skipped otherwise, so the
fallback assertions stay runnable on a machine without either package.
"""

import dataclasses
import time
from types import SimpleNamespace

import pytest

try:  # test-only; the limiter itself imports redis lazily
    import fakeredis
except ImportError:  # pragma: no cover - only reachable without the package
    fakeredis = None

from app import rate_limit
from app.config import settings
from tests.conftest import create_ticket

requires_fakeredis = pytest.mark.skipif(
    fakeredis is None, reason="fakeredis is not installed (pip install fakeredis)"
)

LIMIT = 2
WINDOW_S = 60


@pytest.fixture()
def in_process(monkeypatch):
    """Limiter with no Redis configured: the shipped default."""
    monkeypatch.setattr(rate_limit, "settings", _tight(redis_url=""))


def _tight(**overrides):
    """A 2-per-60s allowance, optionally pointed at Redis."""
    return dataclasses.replace(
        settings,
        ticket_create_rate_limit=LIMIT,
        ticket_create_rate_window_s=WINDOW_S,
        **overrides,
    )


def _fill(key: str, limit: int = LIMIT) -> None:
    for _ in range(limit):
        rate_limit.check(key, limit=limit, window_s=WINDOW_S)


def _clock(monkeypatch, now: float) -> None:
    """Pin the limiter's clock without disturbing the global time module."""
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(monotonic=lambda: now))


# ---------------------------------------------------------------- fallback --


def test_no_redis_url_uses_the_in_process_window(in_process):
    assert rate_limit._redis_client() is None

    _fill("guest")

    assert rate_limit._hits["guest"]  # proof the local branch was the one used
    with pytest.raises(rate_limit.RateLimitExceeded) as exc:
        rate_limit.check("guest", limit=LIMIT, window_s=WINDOW_S)
    assert exc.value.retry_after_s > 0


def test_in_process_window_expires_hits(in_process, monkeypatch):
    """Rewinding the clock past the window frees the allowance again."""
    now = 10_000.0
    _clock(monkeypatch, now)

    _fill("guest")
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("guest", limit=LIMIT, window_s=WINDOW_S)

    _clock(monkeypatch, now + WINDOW_S + 1)
    rate_limit.check("guest", limit=LIMIT, window_s=WINDOW_S)


def test_zero_limit_disables_the_limiter():
    """Disabled is disabled on both branches: no key, no connection attempt."""
    rate_limit.check("guest", limit=0, window_s=WINDOW_S)
    rate_limit.check("guest", limit=0, window_s=WINDOW_S)

    assert "guest" not in rate_limit._hits


def test_unset_redis_is_not_retried_on_every_call(in_process):
    """The negative answer is sticky: no connect cost per request."""
    assert rate_limit._redis_client() is None
    _fill("guest", limit=1)
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("guest", limit=1, window_s=WINDOW_S)
    assert rate_limit._redis_client() is None


# ------------------------------------------------------------------- redis --


@pytest.fixture()
def redis_backed(monkeypatch):
    """Limiter pointed at one shared fake Redis instance, empty at the start."""
    if fakeredis is None:
        pytest.skip("fakeredis is not installed (pip install fakeredis)")
    fake = fakeredis.FakeRedis()
    fake.flushall()
    monkeypatch.setattr(rate_limit, "_new_client", lambda url: fake)
    monkeypatch.setattr(rate_limit, "settings", _tight(redis_url="redis://fakeredis"))
    return fake


def test_redis_backend_enforces_the_allowance(redis_backed):
    """A configured Redis enforces the same policy the local branch does."""
    _fill("enforce")

    with pytest.raises(rate_limit.RateLimitExceeded) as exc:
        rate_limit.check("enforce", limit=LIMIT, window_s=WINDOW_S)
    assert exc.value.retry_after_s > 0
    assert redis_backed.zcard(rate_limit.KEY_PREFIX + "enforce") == LIMIT


def test_rejected_hit_does_not_consume_the_budget(redis_backed):
    """A denial must not push the oldest hit out for everyone else on the key."""
    _fill("budget")
    for _ in range(3):
        with pytest.raises(rate_limit.RateLimitExceeded):
            rate_limit.check("budget", limit=LIMIT, window_s=WINDOW_S)

    assert redis_backed.zcard(rate_limit.KEY_PREFIX + "budget") == LIMIT


def test_redis_backend_window_expires(redis_backed):
    """Age the stored scores past the window instead of sleeping."""
    _fill("expiry")
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("expiry", limit=LIMIT, window_s=WINDOW_S)

    key = rate_limit.KEY_PREFIX + "expiry"
    stale = time.time() - WINDOW_S - 1
    redis_backed.delete(key)
    redis_backed.zadd(key, {"a": stale, "b": stale})

    rate_limit.check("expiry", limit=LIMIT, window_s=WINDOW_S)  # free again


def test_zero_limit_disables_the_limiter_with_redis(redis_backed):
    """A disabled limiter with Redis configured must not touch Redis at all."""
    rate_limit.check("off", limit=0, window_s=WINDOW_S)
    rate_limit.check("off", limit=0, window_s=WINDOW_S)

    assert redis_backed.keys("*") == []


def test_redis_backend_shares_one_allowance_across_instances(monkeypatch):
    """Two separate clients on one server see the same counter.

    This is the property a per-process dict cannot have: each client stands in
    for a different worker, replica, or pod.
    """
    if fakeredis is None:
        pytest.skip("fakeredis is not installed (pip install fakeredis)")
    server = fakeredis.FakeServer()
    instances = [
        fakeredis.FakeRedis(server=server),
        fakeredis.FakeRedis(server=server),
    ]
    monkeypatch.setattr(rate_limit, "settings", _tight(redis_url="redis://fakeredis"))

    # The limiter resolves (and caches) one client per process, so each
    # process gets its own connection to the same server — reset() is what a
    # fresh process starts with.
    resolved = iter(instances)
    monkeypatch.setattr(rate_limit, "_new_client", lambda url: next(resolved))

    _fill("shared")  # process A spends the whole allowance
    rate_limit.reset()
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("shared", limit=LIMIT, window_s=WINDOW_S)  # process B

    # The counter process B read lives on the server, not in either process.
    assert instances[0].zcard(rate_limit.KEY_PREFIX + "shared") == LIMIT
    assert instances[1].zcard(rate_limit.KEY_PREFIX + "shared") == LIMIT


def test_unreachable_redis_falls_back_to_in_process(monkeypatch):
    """Nothing listens on port 1: the local window must carry the limit."""
    monkeypatch.setattr(rate_limit, "settings", _tight(redis_url="redis://127.0.0.1:1/0"))

    _fill("fallback")
    assert rate_limit._hits["fallback"]
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("fallback", limit=LIMIT, window_s=WINDOW_S)


def test_missing_redis_package_falls_back_to_in_process(monkeypatch):
    """REDIS_URL set but 'redis' not installed: ImportError must be survivable."""
    monkeypatch.setattr(rate_limit, "settings", _tight(redis_url="redis://unused"))
    monkeypatch.setattr(rate_limit, "_import_redis", lambda: None)

    assert rate_limit._new_client("redis://unused") is None
    assert rate_limit._redis_client() is None

    _fill("no-package")
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("no-package", limit=LIMIT, window_s=WINDOW_S)


@requires_fakeredis
def test_redis_error_mid_check_falls_back(monkeypatch):
    """An outage while checking must not fail the request it guards."""
    import redis

    class Outage:
        def pipeline(self, *args, **kwargs):
            raise redis.exceptions.ConnectionError("simulated outage")

    monkeypatch.setattr(rate_limit, "settings", _tight(redis_url="redis://fakeredis"))
    monkeypatch.setattr(rate_limit, "_new_client", lambda url: Outage())

    _fill("outage")
    assert rate_limit._hits["outage"]  # the local branch absorbed the failure
    with pytest.raises(rate_limit.RateLimitExceeded):
        rate_limit.check("outage", limit=LIMIT, window_s=WINDOW_S)


# ------------------------------------------------------- end to end (HTTP) --


@requires_fakeredis
def test_guest_ticket_endpoint_uses_the_redis_backend(client, monkeypatch):
    """The call site keeps its signature and passes through the Redis branch."""
    from app.api import tickets as tickets_api

    tight = _tight(redis_url="redis://fakeredis")
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(tickets_api, "settings", tight)
    monkeypatch.setattr(rate_limit, "settings", tight)
    monkeypatch.setattr(rate_limit, "_new_client", lambda url: fake)

    assert create_ticket(client, email="one@example.com").status_code == 201
    assert create_ticket(client, email="two@example.com").status_code == 201
    blocked = create_ticket(client, email="three@example.com")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    # The 429 came from the distributed counter, not from a local dictionary.
    peer = fake.keys(f"{rate_limit.KEY_PREFIX}ticket-create:*")
    assert peer, fake.keys("*")
