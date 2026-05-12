"""Integration tests for ``coworker.connectors.xpm_client.XPMClient``.

Pattern matches ``test_graph_auth.py``: real DB, mocked HTTP via
respx. Tests in this file cover Phase 3E-2 (OAuth refresh scaffolding
only); read/write XPM methods land in later sub-phases with their
own tests.
"""
import asyncio
import datetime as _dt
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorTransient,
)
from coworker.connectors.xpm_client import XPMClient
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import decrypt_str, encrypt_str

_TOKEN_URL = "https://identity.xero.com/connect/token"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def xpm_environment(test_database_url):
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
            asyncio.run(_delete_test_firm(sessionmaker, firm_id))
        asyncio.run(engine.dispose())


async def _delete_test_firm(sessionmaker, firm_id: uuid.UUID) -> None:
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


def _seed_firm(
    sessionmaker,
    *,
    slug: str,
    xpm_client_id: str | None = "xpm-client-id",
    xpm_client_secret: str | None = "xpm-secret-123",
    xpm_refresh_token: str | None = "refresh-tok-1",
    xpm_access_token: str | None = "access-tok-1",
    expires_at: _dt.datetime | None = None,
) -> uuid.UUID:
    """Seed a firm with XPM credentials. Returns firm_id."""

    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            kwargs: dict = {
                "id": firm_id,
                "name": "XPM Test Firm",
                "slug": slug,
                "xpm_client_id": xpm_client_id,
            }
            if xpm_client_secret is not None:
                kwargs["xpm_client_secret_ciphertext"] = encrypt_str(
                    xpm_client_secret, firm_id=firm_id_str
                )
            if xpm_refresh_token is not None:
                kwargs["xpm_refresh_token_ciphertext"] = encrypt_str(
                    xpm_refresh_token, firm_id=firm_id_str
                )
            if xpm_access_token is not None:
                kwargs["xpm_access_token_ciphertext"] = encrypt_str(
                    xpm_access_token, firm_id=firm_id_str
                )
            if expires_at is not None:
                kwargs["xpm_token_expires_at"] = expires_at
            session.add(Firm(**kwargs))
            await session.commit()
            return firm_id

    return asyncio.run(_run())


def _audit_entries(sessionmaker, firm_id: uuid.UUID) -> list[AuditLogEntry]:
    async def _run() -> list[AuditLogEntry]:
        async with sessionmaker() as session, firm_context(firm_id):
            result = await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.firm_id == firm_id)
                .order_by(AuditLogEntry.id.asc())
            )
            return list(result.scalars().all())

    return asyncio.run(_run())


def _load_firm(sessionmaker, firm_id: uuid.UUID) -> Firm:
    async def _run() -> Firm:
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return firm

    return asyncio.run(_run())


def _run_with_firm(sessionmaker, firm_id, body):
    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return await body(session, firm)

    return asyncio.run(_run())


def _token_response(
    *,
    access_token: str = "new-access",
    refresh_token: str = "new-refresh",
    expires_in: int = 1800,
) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": expires_in,
        "token_type": "Bearer",
        "scope": "practicemanager offline_access",
    }


# =========================================================================
# _refresh_access_token
# =========================================================================


def test_refresh_persists_rotated_tokens_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=_token_response(
                        access_token="rotated-access",
                        refresh_token="rotated-refresh",
                        expires_in=1800,
                    ),
                )
            )
            access = await client._refresh_access_token()
        # Verify the wire request.
        sent = route.calls.last.request
        body_str = sent.read().decode()
        assert "grant_type=refresh_token" in body_str
        assert "refresh_token=refresh-tok-1" in body_str
        # Basic auth with client_id:client_secret
        assert sent.headers["Authorization"].startswith("Basic ")
        return access

    new_access = _run_with_firm(sm, firm_id, body)
    assert new_access == "rotated-access"

    # The firm row was mutated and committed. Re-read.
    firm = _load_firm(sm, firm_id)
    assert firm.xpm_access_token_ciphertext is not None
    assert firm.xpm_refresh_token_ciphertext is not None
    firm_id_str = str(firm_id)
    assert decrypt_str(
        firm.xpm_access_token_ciphertext, firm_id=firm_id_str
    ) == "rotated-access"
    assert decrypt_str(
        firm.xpm_refresh_token_ciphertext, firm_id=firm_id_str
    ) == "rotated-refresh"
    assert firm.xpm_token_expires_at is not None
    assert firm.xpm_token_expires_at > _dt.datetime.now(_dt.UTC) + _dt.timedelta(
        minutes=20
    )

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.token_refreshed"]
    assert len(success) == 1
    assert success[0].actor_type == "system"
    assert success[0].actor_id == "system"
    assert success[0].payload["expires_in"] == 1800


def test_refresh_400_invalid_grant_raises_auth_error_and_audits(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-400-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )
            with pytest.raises(ConnectorAuthError):
                await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_400"
    assert failed[0].actor_type == "system"


def test_refresh_5xx_raises_transient_and_audits(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(ConnectorTransient):
                await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "xero_5xx"


def test_refresh_network_error_raises_transient_and_audits(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(side_effect=httpx.ConnectError("no net"))
            with pytest.raises(ConnectorTransient):
                await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


def test_refresh_missing_refresh_token_raises_auth_error(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-norefresh-{uuid.uuid4().hex[:8]}",
        xpm_refresh_token=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ConnectorAuthError):
            await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "missing_refresh_token"


def test_refresh_missing_client_credentials_raises_auth_error(
    xpm_environment,
) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-nocreds-{uuid.uuid4().hex[:8]}",
        xpm_client_id=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with pytest.raises(ConnectorAuthError):
            await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "xpm.token_refresh_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "missing_client_credentials"


def test_refresh_with_user_actor_records_user_actor_on_audit(
    xpm_environment,
) -> None:
    """A UI-initiated refresh records actor_type='user' and actor_id=user_id."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"xpm-user-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    user_id = "fake-user-uuid-string"

    async def body(session, firm):
        client = XPMClient(
            firm, session=session, actor_id=user_id, actor_type="user"
        )
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            await client._refresh_access_token()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "xpm.token_refreshed"]
    assert len(success) == 1
    assert success[0].actor_type == "user"
    assert success[0].actor_id == user_id


# =========================================================================
# _ensure_access_token (cache vs refresh decision)
# =========================================================================


def test_ensure_access_token_uses_cached_token_when_valid(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    # Seeded with an access token valid for another hour — should not refresh.
    firm_id = _seed_firm(
        sm,
        slug=f"xpm-cached-{uuid.uuid4().hex[:8]}",
        xpm_access_token="cached-access",
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        # respx with no mocks: any HTTP attempt raises.
        with respx.mock():
            token = await client._ensure_access_token()
        return token

    token = _run_with_firm(sm, firm_id, body)
    assert token == "cached-access"

    # No audit rows: no refresh happened.
    audits = _audit_entries(sm, firm_id)
    assert not any(a.action == "xpm.token_refreshed" for a in audits)


def test_ensure_access_token_refreshes_when_within_buffer(xpm_environment) -> None:
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    # 30 seconds from expiry — well inside the 5-min refresh buffer.
    firm_id = _seed_firm(
        sm,
        slug=f"xpm-soonexpire-{uuid.uuid4().hex[:8]}",
        xpm_access_token="stale-access",
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=30),
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=_token_response(access_token="freshly-refreshed"),
                )
            )
            return await client._ensure_access_token()

    token = _run_with_firm(sm, firm_id, body)
    assert token == "freshly-refreshed"


def test_ensure_access_token_refreshes_when_expiry_missing(xpm_environment) -> None:
    """Firm with no recorded expiry triggers a refresh — incomplete onboarding."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-noexp-{uuid.uuid4().hex[:8]}",
        xpm_access_token="any",
        expires_at=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            return await client._ensure_access_token()

    _run_with_firm(sm, firm_id, body)


def test_ensure_access_token_refreshes_when_access_ciphertext_missing(
    xpm_environment,
) -> None:
    """Firm has expires_at but no access ciphertext — inconsistent row, refresh."""
    sm = xpm_environment["sessionmaker"]
    created = xpm_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"xpm-noaccess-{uuid.uuid4().hex[:8]}",
        xpm_access_token=None,  # but expires_at still set far in future
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
    )
    created.append(firm_id)

    async def body(session, firm):
        client = XPMClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_TOKEN_URL).mock(
                return_value=httpx.Response(200, json=_token_response())
            )
            return await client._ensure_access_token()

    _run_with_firm(sm, firm_id, body)
