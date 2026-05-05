"""Integration tests for the PKCE state store.

Exercises the real Redis client against a dedicated test DB (number 15
on the same instance) so we never touch dev OAuth state. The fixture
flushdb's that database before and after each test for isolation.
"""
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
import pytest_asyncio
from redis.asyncio import Redis, from_url

from coworker.config import get_settings
from coworker.security.oauth_state import (
    OAuthStateError,
    consume_state,
    create_state,
)


_TEST_REDIS_DB = "/15"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


@pytest_asyncio.fixture
async def redis_test_client(monkeypatch) -> Redis:
    """Redirect coworker.security.oauth_state's get_redis to a test client.

    Uses a different Redis logical DB from dev. Flushes before and after
    so tests are isolated even if a previous run left keys behind.
    """
    client = from_url(_test_redis_url(), encoding="utf-8", decode_responses=True)
    await client.flushdb()

    # Patch at both the definition site and the import site. oauth_state
    # does `from coworker.db.redis import get_redis`, which binds the
    # symbol into oauth_state's own namespace; patching only the source
    # module would leave that local binding pointing at the original
    # lru-cached client and tests would silently share a connection
    # across event loops ("Future attached to a different loop").
    from coworker.db import redis as redis_module
    from coworker.security import oauth_state as oauth_state_module

    monkeypatch.setattr(redis_module, "get_redis", lambda: client)
    monkeypatch.setattr(oauth_state_module, "get_redis", lambda: client)

    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.mark.asyncio
async def test_create_and_consume_roundtrip(redis_test_client: Redis) -> None:
    firm_id = uuid.uuid4()
    state_token, code_verifier = await create_state(firm_id)

    assert state_token
    assert code_verifier
    assert len(code_verifier) >= 43, "PKCE verifier must be 43+ chars (RFC 7636)"

    consumed_firm_id, consumed_verifier = await consume_state(state_token)
    assert consumed_firm_id == firm_id
    assert consumed_verifier == code_verifier


@pytest.mark.asyncio
async def test_consume_missing_token_raises(redis_test_client: Redis) -> None:
    with pytest.raises(OAuthStateError):
        await consume_state("never-issued-token")


@pytest.mark.asyncio
async def test_consume_replay_fails_on_second_call(redis_test_client: Redis) -> None:
    firm_id = uuid.uuid4()
    state_token, _ = await create_state(firm_id)

    # First consume succeeds
    await consume_state(state_token)

    # Second consume of the same token must fail (atomic GETDEL deleted it)
    with pytest.raises(OAuthStateError):
        await consume_state(state_token)


@pytest.mark.asyncio
async def test_consume_malformed_token_raises_without_disclosure(
    redis_test_client: Redis,
) -> None:
    """Empty / weird input raises a generic OAuthStateError. The error
    message must not echo the supplied token (a buggy log line that
    captures the exception message could otherwise leak still-valid
    state).
    """
    weird_inputs = [
        "",
        "   ",
        "../../etc/passwd",
        "oauth:state:not-actually-mine",
        "\x00\x01\x02",
    ]
    for token in weird_inputs:
        with pytest.raises(OAuthStateError) as excinfo:
            await consume_state(token)
        # Strict-equality check rather than `token not in message` because
        # empty/whitespace inputs would trivially substring-match any
        # message. Any future attempt to make the error more "helpful"
        # by echoing the token would fail this assertion.
        assert str(excinfo.value) == "invalid or expired state", (
            f"OAuthStateError must use the canonical generic message; "
            f"got {excinfo.value!r}"
        )
