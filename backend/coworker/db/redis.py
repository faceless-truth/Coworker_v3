"""Lazy Redis client factory.

Mirrors the get_engine / get_sessionmaker shape in coworker.db.session:
constructs a single async Redis client on first use and caches it. The
factory is patchable from tests via monkeypatch on this module.

decode_responses=True means GET / SET round-trips strings rather than
bytes, which suits our use cases (JSON payloads, signed tokens). If a
caller needs raw bytes (e.g. binary blobs), build its own client.
"""
from functools import lru_cache

from redis.asyncio import Redis, from_url

from coworker.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    settings = get_settings()
    return from_url(
        str(settings.REDIS_URL),
        encoding="utf-8",
        decode_responses=True,
    )
