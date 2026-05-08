"""Integration tests for `coworker.connectors.shadow_mode.guard_writable`.

Direct-call tests against the real test DB: seed a firm with shadow_mode
True or False, call guard_writable under firm_context, assert on
exception type and audit-log contents. No HTTP, no FastAPI machinery.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.connectors.shadow_mode import (
    ShadowModeBlocked,
    guard_writable,
)
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest.fixture
def shadow_environment(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sessionmaker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    created_firm_ids: list[uuid.UUID] = []
    try:
        yield {"sessionmaker": sessionmaker, "created_firm_ids": created_firm_ids}
    finally:
        for firm_id in created_firm_ids:
            asyncio.run(_delete_firm(sessionmaker, firm_id))
        asyncio.run(engine.dispose())


async def _delete_firm(sessionmaker, firm_id: uuid.UUID) -> None:
    tables = ("firms", "users", "audit_log")
    async with sessionmaker() as session:
        for t in tables:
            await session.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
        try:
            await session.execute(
                text("DELETE FROM audit_log WHERE firm_id = :id"),
                {"id": str(firm_id)},
            )
            await session.execute(
                text("DELETE FROM firms WHERE id = :id"), {"id": str(firm_id)}
            )
            await session.commit()
        finally:
            for t in tables:
                await session.execute(
                    text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
                )
            await session.commit()


def _seed_firm(sessionmaker, *, slug: str, shadow_mode: bool) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Shadow Test Firm",
                    slug=slug,
                    shadow_mode=shadow_mode,
                )
            )
            await session.commit()
            return firm_id

    return asyncio.run(_run())


def _audits(sessionmaker, firm_id: uuid.UUID) -> list[AuditLogEntry]:
    async def _run() -> list[AuditLogEntry]:
        async with sessionmaker() as session, firm_context(firm_id):
            result = await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.firm_id == firm_id)
                .order_by(AuditLogEntry.id.asc())
            )
            return list(result.scalars().all())

    return asyncio.run(_run())


# --------------------------- shadow=True blocks -----------------------------


def test_shadow_mode_true_blocks_and_audits(shadow_environment) -> None:
    sm = shadow_environment["sessionmaker"]
    created = shadow_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm, slug=f"shadow-true-{uuid.uuid4().hex[:8]}", shadow_mode=True
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            with pytest.raises(ShadowModeBlocked) as excinfo:
                await guard_writable(
                    session, firm, action="email.create_draft",
                    actor_id="user-123", actor_type="user",
                )
            assert excinfo.value.action == "email.create_draft"

    asyncio.run(_run())

    audits = _audits(sm, firm_id)
    blocks = [
        a for a in audits if a.action == "shadow_blocked.email.create_draft"
    ]
    assert len(blocks) == 1
    assert blocks[0].payload == {
        "action": "email.create_draft",
        "actor_id": "user-123",
    }
    assert blocks[0].actor_id == "user-123"
    assert blocks[0].actor_type == "user"


# --------------------------- shadow=False passes ----------------------------


def test_shadow_mode_false_returns_silently(shadow_environment) -> None:
    sm = shadow_environment["sessionmaker"]
    created = shadow_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm, slug=f"shadow-false-{uuid.uuid4().hex[:8]}", shadow_mode=False
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            # Should return None, not raise.
            result = await guard_writable(
                session, firm, action="email.create_draft"
            )
            assert result is None

    asyncio.run(_run())

    # No audit row for a no-op pass.
    audits = _audits(sm, firm_id)
    assert not any(
        a.action.startswith("shadow_blocked.") for a in audits
    )


def test_shadow_mode_blocked_carries_action_attribute() -> None:
    """Pure-Python: ShadowModeBlocked exposes the action."""
    err = ShadowModeBlocked(action="fusesign.create_envelope")
    assert err.action == "fusesign.create_envelope"
    assert "fusesign.create_envelope" in str(err)


def test_shadow_mode_default_actor_is_system(shadow_environment) -> None:
    """When no actor_id is passed, audit shows actor_id='system'."""
    sm = shadow_environment["sessionmaker"]
    created = shadow_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm, slug=f"shadow-sys-{uuid.uuid4().hex[:8]}", shadow_mode=True
    )
    created.append(firm_id)

    async def _run() -> None:
        async with sm() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            with pytest.raises(ShadowModeBlocked):
                await guard_writable(
                    session, firm, action="xpm.create_client_note"
                )

    asyncio.run(_run())

    audits = _audits(sm, firm_id)
    blocks = [
        a for a in audits
        if a.action == "shadow_blocked.xpm.create_client_note"
    ]
    assert len(blocks) == 1
    assert blocks[0].actor_id == "system"
    assert blocks[0].actor_type == "system"
