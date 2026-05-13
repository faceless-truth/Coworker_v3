"""Integration tests for ``coworker.connectors.rate_limit_redis``.

Real Redis (logical DB 11) so the Lua script runs against the same
implementation production will use. Each test gets a freshly-flushed
DB and uses unique scope_keys to avoid cross-test contamination.
"""
import asyncio
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from redis.asyncio import Redis, from_url

from coworker.config import get_settings
from coworker.connectors.exceptions import ConnectorRateLimited
from coworker.connectors.rate_limit_redis import (
    RedisRateLimiter,
    SlidingWindowLimit,
)

_TEST_REDIS_DB = "/11"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


@pytest_asyncio.fixture
async def redis_client():
    client = from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


# =========================================================================
# Single window
# =========================================================================


async def test_acquire_under_capacity_succeeds(redis_client: Redis) -> None:
    limiter = RedisRateLimiter(redis_client)
    limit = SlidingWindowLimit(
        scope_key="mailbox:m1", label="per_minute",
        capacity=5, window_seconds=60,
    )
    # 5 acquires must all succeed.
    for _ in range(5):
        await limiter.acquire([limit])


async def test_acquire_over_capacity_raises_rate_limited(
    redis_client: Redis,
) -> None:
    limiter = RedisRateLimiter(redis_client)
    limit = SlidingWindowLimit(
        scope_key="mailbox:m2", label="per_minute",
        capacity=3, window_seconds=60,
    )
    for _ in range(3):
        await limiter.acquire([limit])

    with pytest.raises(ConnectorRateLimited) as excinfo:
        await limiter.acquire([limit])
    # retry_after should be a positive float <= window_seconds.
    assert excinfo.value.retry_after is not None
    assert 0.0 <= excinfo.value.retry_after <= 60.0


async def test_acquire_recovers_after_window_passes(redis_client: Redis) -> None:
    """Use a tiny window so the test isn't slow; verify slots free up."""
    limiter = RedisRateLimiter(redis_client)
    limit = SlidingWindowLimit(
        scope_key="mailbox:m3", label="per_window",
        capacity=2, window_seconds=1,
    )
    await limiter.acquire([limit])
    await limiter.acquire([limit])
    with pytest.raises(ConnectorRateLimited):
        await limiter.acquire([limit])

    # Wait long enough for the window to slide.
    await asyncio.sleep(1.1)
    # Now another acquire must succeed.
    await limiter.acquire([limit])


async def test_acquire_empty_limits_is_noop(redis_client: Redis) -> None:
    """Passing no limits returns silently — convenient for tests."""
    limiter = RedisRateLimiter(redis_client)
    await limiter.acquire([])
    # And does not touch Redis.
    keys = await redis_client.keys("ratelimit:*")
    assert keys == []


# =========================================================================
# Multiple windows
# =========================================================================


async def test_multiple_windows_both_must_pass(redis_client: Redis) -> None:
    """A pass requires every window has headroom. Fail any → denied."""
    limiter = RedisRateLimiter(redis_client)
    minute = SlidingWindowLimit(
        scope_key="mailbox:m4", label="per_minute",
        capacity=10, window_seconds=60,
    )
    hour = SlidingWindowLimit(
        scope_key="mailbox:m4", label="per_hour",
        capacity=2, window_seconds=3600,
    )

    # Two acquires succeed (both windows have headroom).
    await limiter.acquire([minute, hour])
    await limiter.acquire([minute, hour])

    # Third would breach the hour window (capacity=2) even though
    # minute has plenty of headroom.
    with pytest.raises(ConnectorRateLimited) as excinfo:
        await limiter.acquire([minute, hour])
    # retry_after should reflect the hour window (the binding one).
    assert excinfo.value.retry_after is not None
    assert excinfo.value.retry_after > 1.0  # well over the minute window


async def test_denial_does_not_record_in_either_window(
    redis_client: Redis,
) -> None:
    """When acquire is denied no window is mutated — the rejected event
    must not be counted against the user's quota.
    """
    limiter = RedisRateLimiter(redis_client)
    binding = SlidingWindowLimit(
        scope_key="mailbox:m5", label="per_minute",
        capacity=1, window_seconds=60,
    )
    second_window = SlidingWindowLimit(
        scope_key="mailbox:m5", label="per_hour",
        capacity=10, window_seconds=3600,
    )
    await limiter.acquire([binding, second_window])
    # Confirm one in each window so far.
    assert await redis_client.zcard("ratelimit:mailbox:m5:per_minute") == 1
    assert await redis_client.zcard("ratelimit:mailbox:m5:per_hour") == 1

    with pytest.raises(ConnectorRateLimited):
        await limiter.acquire([binding, second_window])

    # Denial must NOT have added a second event to either window.
    assert await redis_client.zcard("ratelimit:mailbox:m5:per_minute") == 1
    assert await redis_client.zcard("ratelimit:mailbox:m5:per_hour") == 1


async def test_scope_keys_are_isolated(redis_client: Redis) -> None:
    """Different scope keys don't share quota even with the same label."""
    limiter = RedisRateLimiter(redis_client)
    a = SlidingWindowLimit(
        scope_key="mailbox:a", label="per_minute",
        capacity=1, window_seconds=60,
    )
    b = SlidingWindowLimit(
        scope_key="mailbox:b", label="per_minute",
        capacity=1, window_seconds=60,
    )
    await limiter.acquire([a])
    # Mailbox A is now full, but B has its own quota.
    await limiter.acquire([b])
    # Re-acquiring A is denied.
    with pytest.raises(ConnectorRateLimited):
        await limiter.acquire([a])


async def test_key_ttl_is_set_so_idle_keys_expire(redis_client: Redis) -> None:
    """The script PEXPIREs each window key to slightly more than its
    window size so idle Redis keys don't pile up forever.
    """
    limiter = RedisRateLimiter(redis_client)
    limit = SlidingWindowLimit(
        scope_key="mailbox:m6", label="per_minute",
        capacity=5, window_seconds=60,
    )
    await limiter.acquire([limit])
    ttl = await redis_client.pttl("ratelimit:mailbox:m6:per_minute")
    # Should be within the window+1s ceiling.
    assert 1 < ttl <= 61_000
