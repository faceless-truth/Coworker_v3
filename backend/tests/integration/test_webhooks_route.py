"""End-to-end tests for the Graph webhook receiver.

The route is driven through FastAPI's TestClient; Redis is the
real test instance (logical DB 9) so the enqueue side of the
contract is exercised. Microsoft's POST body shapes are faked to
match the documented notification schema.
"""
import asyncio
import json
import uuid
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from fastapi.testclient import TestClient
from redis.asyncio import from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.api.main import app
from coworker.config import get_settings
from coworker.db.models import Firm
from coworker.db.session import _attach_pool_listeners, firm_context

_TEST_REDIS_DB = "/9"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


def _fresh_test_redis():
    return from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )


async def _redis_flushdb_oneshot() -> None:
    client = _fresh_test_redis()
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def webhook_env(test_database_url, monkeypatch):
    """Wire SessionLocal + Redis + Engine to test instances and seed a firm."""
    from coworker.db import redis as redis_module
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)

    redis_module.get_redis.cache_clear()
    monkeypatch.setattr(redis_module, "get_redis", _fresh_test_redis)

    await _redis_flushdb_oneshot()

    firm_id = uuid.uuid4()
    slug = f"webhook-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Webhook Firm", slug=slug))
        await session.commit()

    try:
        yield {"sm": sm, "firm_id": firm_id, "slug": slug}
    finally:
        await _cleanup_firm(sm, firm_id)
        await _redis_flushdb_oneshot()
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = ("firms", "users", "audit_log")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM users WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"),
                {"id": str(firm_id)},
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


def _notification(message_id: str = "msg-1", change_type: str = "created") -> dict:
    return {
        "subscriptionId": "sub-123",
        "clientState": "secret",
        "changeType": change_type,
        "resource": "users/u-1/messages/" + message_id,
        "resourceData": {
            "@odata.type": "#Microsoft.Graph.Message",
            "id": message_id,
        },
    }


def _queue_contents() -> list[dict]:
    """Snapshot of the test Redis queue."""

    async def _run() -> list[dict]:
        client = _fresh_test_redis()
        try:
            raw = await client.lrange("queue:plugin_events", 0, -1)
            return [json.loads(r) for r in raw]
        finally:
            await client.aclose()

    return asyncio.run(_run())


# ===========================================================================
# Tests
# ===========================================================================


def test_validation_token_handshake_returns_plain_text(webhook_env) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(
        f"/webhooks/graph/{slug}",
        params={"validationToken": "abc-token-xyz"},
    )
    assert resp.status_code == 200
    assert resp.text == "abc-token-xyz"
    # No enqueue on handshake.
    assert _queue_contents() == []


def test_notification_enqueues_plugin_event(webhook_env) -> None:
    slug = webhook_env["slug"]
    firm_id = webhook_env["firm_id"]

    body = {"value": [_notification(message_id="msg-real-1")]}

    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 1
    e = events[0]
    assert e["trigger"] == "email_received"
    assert e["firm_slug"] == slug
    assert e["firm_id"] == str(firm_id)
    assert e["event_data"]["message_id"] == "msg-real-1"
    assert e["event_data"]["change_type"] == "created"
    assert e["event_data"]["subscription_id"] == "sub-123"


def test_multiple_notifications_in_one_post_all_enqueue(webhook_env) -> None:
    slug = webhook_env["slug"]
    body = {
        "value": [
            _notification(message_id=f"msg-{i}") for i in range(3)
        ]
    }

    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 3
    assert {e["event_data"]["message_id"] for e in events} == {
        "msg-0", "msg-1", "msg-2",
    }


def test_unknown_slug_returns_202_without_enqueuing(webhook_env) -> None:
    client = TestClient(app)
    resp = client.post(
        f"/webhooks/graph/does-not-exist-{uuid.uuid4().hex[:6]}",
        json={"value": [_notification()]},
    )
    # 202 (no leak of slug existence), but nothing enqueued.
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_malformed_json_returns_202(webhook_env) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(
        f"/webhooks/graph/{slug}",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_non_object_body_returns_202(webhook_env) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=["just", "an", "array"])
    assert resp.status_code == 202
    assert _queue_contents() == []


def test_notification_missing_message_id_is_skipped(webhook_env) -> None:
    """A notification lacking resourceData.id is dropped silently."""
    slug = webhook_env["slug"]
    body = {
        "value": [
            {"subscriptionId": "sub", "changeType": "created"},  # no resourceData
            _notification(message_id="msg-good"),
        ]
    }
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json=body)
    assert resp.status_code == 202

    events = _queue_contents()
    assert len(events) == 1
    assert events[0]["event_data"]["message_id"] == "msg-good"


def test_empty_notifications_array_returns_202_without_enqueueing(
    webhook_env,
) -> None:
    slug = webhook_env["slug"]
    client = TestClient(app)
    resp = client.post(f"/webhooks/graph/{slug}", json={"value": []})
    assert resp.status_code == 202
    assert _queue_contents() == []
