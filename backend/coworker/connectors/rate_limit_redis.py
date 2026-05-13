"""Redis-backed sliding-window rate limiter.

Replaces the per-process in-memory limiter (Phase 3A Step 3) for
multi-worker correctness. The architecture's "per-plugin per-minute,
per-mailbox per-hour, per-mailbox per-day" caps need to hold across
every worker on the droplet (and across droplets when we add a second);
shared state in Redis is the simplest correct way to do that.

Storage shape
-------------

Each window is a Redis sorted set (ZSET) keyed by
``ratelimit:{scope_key}:{label}``. Members are unique event ids
(opaque strings — the implementation uses a uuid4 hex), scores are
event timestamps in milliseconds since the epoch. On every call we
trim entries older than ``now - window_ms``, count what's left, and
either allow + add the new event or deny if the window is full.

Atomicity
---------

A small Lua script does all the work in one round-trip so the
trim-count-add sequence is observed atomically even under high
concurrency. Two-pass design: first we check every window (without
mutation that would need to be rolled back on a later denial), then
once all pass we record the event in every window. ``ConnectorRateLimited``
carries the most-restrictive window's ``retry_after`` so the caller
can schedule a sensible backoff.

API
---

``RedisRateLimiter.acquire(limits)`` takes a list of
``SlidingWindowLimit`` tuples. Each carries its own scope key, so a
single ``acquire`` can mix per-plugin + per-mailbox windows. Pass an
empty list to no-op (useful in tests).

This module does NOT wire itself into the existing connector code —
that integration follows when the orchestrator and plugin layers are
ready to specify their own limits. The in-memory ``coworker.graph.rate_limit``
limiter stays for unit tests and single-process simulation.
"""
import time
import uuid
from typing import NamedTuple

from redis.asyncio import Redis

from coworker.connectors.exceptions import ConnectorRateLimited

_RATELIMIT_PREFIX = "ratelimit"

# Atomic check-all-windows-then-record-all script.
#
# KEYS  — one Redis key per window, in the same order as the limits.
# ARGV  — packed:
#   [1]              now_ms (int)
#   [2]              member (unique string)
#   [3..3+N-1]       capacity per window (int) — same order as KEYS
#   [3+N..3+2N-1]    window_ms per window (int) — same order as KEYS
#
# Returns either {0, oldest_score, denied_window_idx} when denied
# (oldest_score = the score of the oldest in-window event for the
# denied window, used to compute retry_after) or {1} when allowed.
_LUA_SCRIPT = """
local now_ms = tonumber(ARGV[1])
local member = ARGV[2]
local n = #KEYS
for i = 1, n do
  local cap = tonumber(ARGV[2 + i])
  local win = tonumber(ARGV[2 + n + i])
  local cutoff = now_ms - win
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
  local count = redis.call('ZCARD', KEYS[i])
  if count >= cap then
    local oldest = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
    local oldest_score = 0
    if oldest[2] then oldest_score = tonumber(oldest[2]) end
    return {0, oldest_score, i}
  end
end
-- All windows have headroom; record the event in each.
for i = 1, n do
  local win = tonumber(ARGV[2 + n + i])
  redis.call('ZADD', KEYS[i], now_ms, member)
  -- TTL slightly larger than the window so an idle key disappears.
  redis.call('PEXPIRE', KEYS[i], win + 1000)
end
return {1}
"""


class SlidingWindowLimit(NamedTuple):
    """One window in a rate-limit check.

    Attributes:
        scope_key: stable identifier for the entity being limited,
            e.g. ``"plugin:smart_responder:firm:<uuid>"`` or
            ``"mailbox:<user_uuid>"``. The Redis key is derived as
            ``ratelimit:{scope_key}:{label}``.
        label: short window name (``"per_minute"``, ``"per_hour"``,
            ``"per_day"``) — namespaces the scope key so the same
            entity can have multiple windows in flight.
        capacity: max events allowed within the window.
        window_seconds: window size in seconds.
    """

    scope_key: str
    label: str
    capacity: int
    window_seconds: int


class RedisRateLimiter:
    """Multi-window sliding-window rate limiter backed by Redis ZSETs.

    Stateless beyond the Redis client — multiple instances may share
    the same Redis without interfering. Safe for concurrent calls from
    many asyncio tasks because the Lua script runs atomically.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def acquire(self, limits: list[SlidingWindowLimit]) -> None:
        """Check every window; record the event in all, or deny.

        On denial, ``ConnectorRateLimited`` is raised carrying a
        ``retry_after`` (seconds) derived from the most-restrictive
        window's oldest in-flight event: that event will roll off the
        window at ``oldest_score + window_ms``, so waiting for that
        many seconds guarantees the same call would succeed.

        Args:
            limits: per-window configurations. Empty list is a no-op.

        Raises:
            ConnectorRateLimited: any window would be exceeded by
                this event.
        """
        if not limits:
            return

        keys = [
            f"{_RATELIMIT_PREFIX}:{lim.scope_key}:{lim.label}"
            for lim in limits
        ]
        now_ms = int(time.time() * 1000)
        member = uuid.uuid4().hex
        capacities = [str(lim.capacity) for lim in limits]
        window_mss = [str(lim.window_seconds * 1000) for lim in limits]

        argv = [str(now_ms), member, *capacities, *window_mss]
        # redis-py's eval typing returns Awaitable[Any] | Any depending
        # on which client stub is in scope; the narrow ignore matches
        # the pattern in TokenMeter.usage.
        result = await self._redis.eval(  # type: ignore[misc]
            _LUA_SCRIPT, len(keys), *keys, *argv
        )

        if not result or int(result[0]) == 1:
            return

        oldest_score = int(result[1])
        denied_idx = int(result[2]) - 1  # Lua is 1-indexed
        denied = limits[denied_idx]
        # Seconds until the oldest in-window event ages out and frees
        # one slot. clamp to >= 0 for the cosmetic-zero edge case.
        window_ms = denied.window_seconds * 1000
        retry_after_ms = max(0, (oldest_score + window_ms) - now_ms)
        retry_after = retry_after_ms / 1000.0

        raise ConnectorRateLimited(retry_after=retry_after)
