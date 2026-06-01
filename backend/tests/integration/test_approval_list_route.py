"""End-to-end tests for GET /api/v1/approvals (wrapped envelope).

Covers the query-scoped list route added in Task 006: status
filtering, limit cap with total reflecting the full count, 422
on garbage status, and cross-firm isolation.

Reuses the ``routes_env`` fixture pattern from
``test_approval_routes.py`` (per-test engine + monkeypatch,
NullPool, firm_context teardown bracket).
"""
import asyncio
import datetime as _dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import jwt
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.api.main import app
from coworker.approval.items import CreateApprovalInput, create_approval
from coworker.config import get_settings
from coworker.db.models import ApprovalItem, Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def routes_env(test_database_url, monkeypatch) -> AsyncIterator[dict[str, Any]]:
    """Per-test engine + firm + user, with FORCE-RLS teardown bracket."""
    from coworker.db import session as session_module

    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    monkeypatch.setattr(session_module, "get_sessionmaker", lambda: sm)
    monkeypatch.setattr(session_module, "get_engine", lambda: engine)

    firm_id = uuid.uuid4()
    slug = f"appr-list-{uuid.uuid4().hex[:8]}"
    async with sm() as session, firm_context(firm_id):
        firm = Firm(id=firm_id, name="Approval List Firm", slug=slug)
        user = User(
            firm_id=firm_id,
            azure_object_id=f"oid-{uuid.uuid4().hex[:12]}",
            upn=f"u-{uuid.uuid4().hex[:8]}@example.com",
            display_name="Principal",
        )
        session.add_all([firm, user])
        await session.commit()
        user_id = user.id

    try:
        yield {
            "sm": sm,
            "firm_id": firm_id,
            "user_id": user_id,
            "slug": slug,
        }
    finally:
        await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm: async_sessionmaker[AsyncSession], firm_id: uuid.UUID) -> None:
    """Lift FORCE-RLS, scrub firm rows, restore FORCE-RLS.

    Tolerant of partially-seeded state so a mid-test failure can
    still clean up. Matches the pattern in test_approval_routes.py.
    """
    tables = ("firms", "users", "audit_log", "approval_items")
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in ("approval_items", "audit_log", "users"):
                await session.execute(
                    text(f"DELETE FROM {t} WHERE firm_id = :id"),
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


def _issue_jwt(*, user_id: uuid.UUID, firm_id: uuid.UUID) -> str:
    now = _dt.datetime.now(_dt.UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "firm_id": str(firm_id),
            "iat": int(now.timestamp()),
            "exp": int((now + _dt.timedelta(seconds=300)).timestamp()),
        },
        get_settings().SESSION_JWT_SECRET.get_secret_value(),
        algorithm="HS256",
    )


def _client(*, user_id: uuid.UUID, firm_id: uuid.UUID) -> TestClient:
    client = TestClient(app)
    client.cookies.set(
        "coworker_session",
        _issue_jwt(user_id=user_id, firm_id=firm_id),
    )
    return client


async def _seed_item(
    sm: async_sessionmaker[AsyncSession],
    firm_id: uuid.UUID,
    *,
    summary: str = "Draft",
    status: str = "pending",
) -> uuid.UUID:
    """Insert one row, optionally forcing it to a non-pending status.

    create_approval always inserts as ``pending`` (or auto-approved
    on a high-confidence single-signer item); for ``rejected`` /
    ``sent`` / ``dispatch_failed`` we update after insert so we can
    reach those states deterministically without driving the
    transition helpers.
    """
    async with sm() as session, firm_context(firm_id):
        row = await create_approval(
            session, firm_id,
            input=CreateApprovalInput(
                plugin_name="smart_responder",
                category="email_draft",
                summary=summary,
                payload={
                    "to": ["c@example.com"],
                    "subject": "Re: foo",
                    "body_html": "<p>hi</p>",
                },
            ),
        )
        if status != "pending":
            await session.execute(
                update(ApprovalItem)
                .where(ApprovalItem.id == row.id)
                .values(status=status)
            )
        await session.commit()
        return row.id


# ===========================================================================
# Tests
# ===========================================================================


def test_status_pending_filters_correctly(routes_env: dict[str, Any]) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    p = asyncio.run(_seed_item(sm, firm_id, summary="Pending one", status="pending"))
    asyncio.run(_seed_item(sm, firm_id, summary="Approved one", status="approved"))
    asyncio.run(_seed_item(sm, firm_id, summary="Rejected one", status="rejected"))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/approvals?status=pending")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(p)
    assert body["items"][0]["status"] == "pending"


def test_status_approved_filters_correctly(routes_env: dict[str, Any]) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    asyncio.run(_seed_item(sm, firm_id, summary="Pending one", status="pending"))
    a = asyncio.run(_seed_item(sm, firm_id, summary="Approved one", status="approved"))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/approvals?status=approved")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(a)}


def test_status_rejected_filters_correctly(routes_env: dict[str, Any]) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    asyncio.run(_seed_item(sm, firm_id, summary="Pending one", status="pending"))
    r = asyncio.run(_seed_item(sm, firm_id, summary="Rejected one", status="rejected"))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/approvals?status=rejected")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert {item["id"] for item in body["items"]} == {str(r)}


def test_absent_status_returns_all_firm_rows(routes_env: dict[str, Any]) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    p = asyncio.run(_seed_item(sm, firm_id, summary="Pending one", status="pending"))
    a = asyncio.run(_seed_item(sm, firm_id, summary="Approved one", status="approved"))
    r = asyncio.run(_seed_item(sm, firm_id, summary="Rejected one", status="rejected"))
    df = asyncio.run(
        _seed_item(sm, firm_id, summary="Dispatch failed", status="dispatch_failed")
    )

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert {item["id"] for item in body["items"]} == {
        str(p), str(a), str(r), str(df),
    }


def test_limit_caps_items_but_total_is_full_count(routes_env: dict[str, Any]) -> None:
    sm = routes_env["sm"]
    firm_id = routes_env["firm_id"]
    user_id = routes_env["user_id"]
    asyncio.run(_seed_item(sm, firm_id, summary="One", status="pending"))
    asyncio.run(_seed_item(sm, firm_id, summary="Two", status="pending"))
    asyncio.run(_seed_item(sm, firm_id, summary="Three", status="pending"))

    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/approvals?status=pending&limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert len(body["items"]) == 1


def test_invalid_status_returns_422(routes_env: dict[str, Any]) -> None:
    user_id = routes_env["user_id"]
    firm_id = routes_env["firm_id"]
    client = _client(user_id=user_id, firm_id=firm_id)
    resp = client.get("/api/v1/approvals?status=garbage")
    assert resp.status_code == 422


def test_cross_firm_isolation(
    routes_env: dict[str, Any], test_database_url: str
) -> None:
    """Firm B's items must not appear in firm A's list response."""
    sm = routes_env["sm"]
    firm_a_id = routes_env["firm_id"]
    user_a_id = routes_env["user_id"]
    a_item = asyncio.run(
        _seed_item(sm, firm_a_id, summary="A pending", status="pending")
    )

    firm_b_id = uuid.uuid4()
    slug_b = f"appr-list-b-{uuid.uuid4().hex[:8]}"
    b_item: uuid.UUID | None = None
    try:
        async def _seed_firm_b() -> uuid.UUID:
            async with sm() as session, firm_context(firm_b_id):
                firm = Firm(id=firm_b_id, name="Firm B", slug=slug_b)
                session.add(firm)
                await session.commit()
            return await _seed_item(
                sm, firm_b_id, summary="B pending", status="pending"
            )

        b_item = asyncio.run(_seed_firm_b())

        client = _client(user_id=user_a_id, firm_id=firm_a_id)
        resp = client.get("/api/v1/approvals?status=pending")
        assert resp.status_code == 200
        body = resp.json()
        ids = {item["id"] for item in body["items"]}
        assert str(a_item) in ids
        assert str(b_item) not in ids
        assert body["total"] == 1
    finally:
        # Independent of test-body success: scrub firm B even if
        # the assertions failed mid-way.
        asyncio.run(_cleanup_firm(sm, firm_b_id))


