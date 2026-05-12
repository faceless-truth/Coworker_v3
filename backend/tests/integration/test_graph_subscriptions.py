"""Integration tests for ``coworker.graph.subscriptions``.

Two surfaces:

- ``graph_app_context`` — token acquisition via client_credentials,
  cache behaviour, missing-credential ValueErrors, login-endpoint
  error mapping.
- ``subscribe_change_notifications`` / ``renew_subscription`` —
  Graph POST/PATCH against /subscriptions with the standard error
  taxonomy; system-actor audit shape.

The login endpoint URL embeds the firm's azure_tenant_id, so we
mock it with a regex match. The app-token cache is process-global
so an autouse fixture clears it between tests.
"""
import asyncio
import datetime as _dt
import re
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
    ConnectorNotFound,
    ConnectorTransient,
)
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph import subscriptions as subs_module
from coworker.graph.subscriptions import (
    AppGraphContext,
    Subscription,
    graph_app_context,
    renew_subscription,
    subscribe_change_notifications,
)
from coworker.security.encryption import encrypt_str

_LOGIN_URL_RE = re.compile(
    r"^https://login\.microsoftonline\.com/[^/]+/oauth2/v2\.0/token$"
)
_SUBS_URL = "https://graph.microsoft.com/v1.0/subscriptions"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture(autouse=True)
def clear_app_token_cache():
    """Tests must start with an empty cache to assert fetch behaviour."""
    subs_module._app_token_cache.clear()
    yield
    subs_module._app_token_cache.clear()


@pytest.fixture
def graph_subs_environment(test_database_url):
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
    tenant_id: str | None = None,
    client_id: str | None = None,
    secret: str | None = "client-secret",
) -> uuid.UUID:
    """Seed a firm with Azure credentials. Returns firm_id."""

    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            firm_kwargs: dict = {
                "id": firm_id,
                "name": "Subs Test Firm",
                "slug": slug,
                "azure_tenant_id": tenant_id or str(uuid.uuid4()),
                "azure_client_id": client_id or str(uuid.uuid4()),
            }
            if secret is not None:
                firm_kwargs["azure_client_secret_ciphertext"] = encrypt_str(
                    secret, firm_id=firm_id_str
                )
            session.add(Firm(**firm_kwargs))
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


def _run_with_firm(sessionmaker, firm_id, body):
    """Build a session bound to the firm, fetch the firm row, call body(session, firm)."""

    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return await body(session, firm)

    return asyncio.run(_run())


def _token_response(token: str = "app-tok-1", expires_in: int = 3600) -> dict:
    return {
        "access_token": token,
        "expires_in": expires_in,
        "token_type": "Bearer",
        "scope": "https://graph.microsoft.com/.default",
    }


def _subscription_response(
    *,
    sub_id: str = "sub-abc",
    resource: str = "users/u1/mailFolders('Inbox')/messages",
    change_type: str = "created,updated",
    notification_url: str = "https://example.com/hook",
    expiration: str = "2026-05-15T10:00:00.0000000Z",
    client_state: str | None = "secret-state",
) -> dict:
    return {
        "id": sub_id,
        "resource": resource,
        "changeType": change_type,
        "notificationUrl": notification_url,
        "expirationDateTime": expiration,
        "clientState": client_state,
        "applicationId": "azure-app-id",
        "creatorId": "creator-id",
    }


# =========================================================================
# graph_app_context — token acquisition + cache
# =========================================================================


def test_graph_app_context_fetches_token_and_returns_context(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(url__regex=_LOGIN_URL_RE).mock(
                return_value=httpx.Response(200, json=_token_response("tok-1"))
            )
            ctx = await graph_app_context(session, firm)
        assert isinstance(ctx, AppGraphContext)
        assert ctx.access_token == "tok-1"
        assert ctx.firm.id == firm.id
        # Form body should include the client_credentials grant.
        sent = route.calls.last.request
        body_str = sent.read().decode()
        assert "grant_type=client_credentials" in body_str
        assert f"client_id={firm.azure_client_id}" in body_str
        assert "scope=https%3A%2F%2Fgraph.microsoft.com%2F.default" in body_str
        # The decrypted secret value is in the body — confirm it's the
        # plaintext, not the ciphertext bytes.
        assert "client-secret" in body_str

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_uses_cache_on_second_call(
    graph_subs_environment,
) -> None:
    """Second call within the cache TTL doesn't re-hit the login endpoint."""
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-cache-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(url__regex=_LOGIN_URL_RE).mock(
                return_value=httpx.Response(200, json=_token_response("tok-cached"))
            )
            ctx1 = await graph_app_context(session, firm)
            ctx2 = await graph_app_context(session, firm)
        assert ctx1.access_token == ctx2.access_token == "tok-cached"
        # Login endpoint hit exactly once across both context calls.
        assert route.call_count == 1

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_refetches_when_cache_within_refresh_buffer(
    graph_subs_environment,
) -> None:
    """A cache entry near expiry is re-fetched, not reused."""
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-refresh-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    # Plant a near-expiry token directly in the cache.
    subs_module._app_token_cache[str(firm_id)] = (
        "stale-tok",
        _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=30),
    )

    async def body(session, firm):
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(url__regex=_LOGIN_URL_RE).mock(
                return_value=httpx.Response(200, json=_token_response("fresh-tok"))
            )
            ctx = await graph_app_context(session, firm)
        assert ctx.access_token == "fresh-tok"

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_missing_tenant_id_raises(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-notenant-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        # Null out tenant_id on the in-memory firm row to simulate the
        # incomplete-onboarding shape.
        firm.azure_tenant_id = None
        with pytest.raises(ValueError, match="azure_tenant_id"):
            await graph_app_context(session, firm)

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_missing_client_id_raises(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-noclient-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        firm.azure_client_id = None
        with pytest.raises(ValueError, match="azure_client_id"):
            await graph_app_context(session, firm)

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_missing_client_secret_raises(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm, slug=f"app-nosecret-{uuid.uuid4().hex[:8]}", secret=None
    )
    created.append(firm_id)

    async def body(session, firm):
        with pytest.raises(ValueError, match="azure_client_secret"):
            await graph_app_context(session, firm)

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_login_400_raises_auth_error(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-400-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(url__regex=_LOGIN_URL_RE).mock(
                return_value=httpx.Response(
                    400, json={"error": "invalid_client"}
                )
            )
            with pytest.raises(ConnectorAuthError):
                await graph_app_context(session, firm)

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_login_5xx_raises_transient(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(url__regex=_LOGIN_URL_RE).mock(
                return_value=httpx.Response(503)
            )
            with pytest.raises(ConnectorTransient):
                await graph_app_context(session, firm)

    _run_with_firm(sm, firm_id, body)


def test_graph_app_context_login_network_error_raises_transient(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"app-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(url__regex=_LOGIN_URL_RE).mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await graph_app_context(session, firm)

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# subscribe_change_notifications
# =========================================================================


def _seed_and_make_ctx(
    sm, *, slug: str, token: str = "tok"
) -> tuple[uuid.UUID, AppGraphContext]:
    """Seed a firm + plant a token in the cache so subscribe/renew tests
    don't need to mock the login endpoint in every test.
    """
    firm_id = _seed_firm(sm, slug=slug)
    subs_module._app_token_cache[str(firm_id)] = (
        token,
        _dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
    )
    return firm_id, None  # type: ignore[return-value]


def test_subscribe_creates_subscription_and_audits_as_system(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"sub-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    expiration = _dt.datetime(2026, 5, 15, 10, 0, 0, tzinfo=_dt.UTC)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.post(_SUBS_URL).mock(
                return_value=httpx.Response(
                    201,
                    json=_subscription_response(
                        sub_id="sub-1",
                        expiration="2026-05-15T10:00:00Z",
                    ),
                )
            )
            sub = await subscribe_change_notifications(
                ctx,
                resource="users/u1/mailFolders('Inbox')/messages",
                notification_url="https://example.com/hook",
                expiration_date_time=expiration,
                client_state="secret-state",
            )
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer tok"
        body_str = sent.read().decode()
        # client_state goes on the wire (Microsoft echoes it back)
        assert "secret-state" in body_str
        assert "expirationDateTime" in body_str
        return sub

    sub = _run_with_firm(sm, firm_id, body)
    assert isinstance(sub, Subscription)
    assert sub.id == "sub-1"
    assert sub.expiration_date_time.tzinfo is not None

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.subscriptions.subscribe"]
    assert len(success) == 1
    row = success[0]
    assert row.actor_type == "system"
    assert row.actor_id == "system"
    assert row.payload["subscription_id"] == "sub-1"
    assert row.payload["resource"] == "users/u1/mailFolders('Inbox')/messages"
    assert row.payload["notification_url"] == "https://example.com/hook"
    # client_state must NOT appear in the audit payload
    assert "client_state" not in row.payload
    assert "secret-state" not in str(row.payload)


def test_subscribe_401_raises_auth_error_with_system_audit(
    graph_subs_environment,
) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"sub-401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    expiration = _dt.datetime(2026, 5, 15, 10, 0, 0, tzinfo=_dt.UTC)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_SUBS_URL).mock(
                return_value=httpx.Response(401, json={"error": "unauthorized"})
            )
            with pytest.raises(ConnectorAuthError):
                await subscribe_change_notifications(
                    ctx,
                    resource="users/u1/mailFolders('Inbox')/messages",
                    notification_url="https://example.com/hook",
                    expiration_date_time=expiration,
                    client_state="secret-state",
                )

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [
        a for a in audits if a.action == "graph.subscriptions.subscribe_failed"
    ]
    assert len(failed) == 1
    assert failed[0].actor_type == "system"
    assert failed[0].payload["reason"] == "microsoft_401"
    # client_state still not in failure payload
    assert "secret-state" not in str(failed[0].payload)


def test_subscribe_5xx_raises_transient(graph_subs_environment) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"sub-5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    expiration = _dt.datetime(2026, 5, 15, 10, 0, 0, tzinfo=_dt.UTC)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_SUBS_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(ConnectorTransient):
                await subscribe_change_notifications(
                    ctx,
                    resource="users/u1/mailFolders('Inbox')/messages",
                    notification_url="https://example.com/hook",
                    expiration_date_time=expiration,
                    client_state="secret-state",
                )

    _run_with_firm(sm, firm_id, body)


def test_subscribe_network_error_audits_as_system(graph_subs_environment) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"sub-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    expiration = _dt.datetime(2026, 5, 15, 10, 0, 0, tzinfo=_dt.UTC)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.post(_SUBS_URL).mock(side_effect=httpx.ConnectError("no net"))
            with pytest.raises(ConnectorTransient):
                await subscribe_change_notifications(
                    ctx,
                    resource="users/u1/mailFolders('Inbox')/messages",
                    notification_url="https://example.com/hook",
                    expiration_date_time=expiration,
                    client_state="secret-state",
                )

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [
        a for a in audits if a.action == "graph.subscriptions.subscribe_failed"
    ]
    assert len(failed) == 1
    assert failed[0].actor_type == "system"
    assert failed[0].payload["reason"] == "network_error"


def test_subscribe_rejects_invalid_inputs(graph_subs_environment) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"sub-input-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    expiration = _dt.datetime(2026, 5, 15, 10, 0, 0, tzinfo=_dt.UTC)
    naive = _dt.datetime(2026, 5, 15, 10, 0, 0)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with pytest.raises(ValueError):
            await subscribe_change_notifications(
                ctx, resource="", notification_url="https://x",
                expiration_date_time=expiration, client_state="x",
            )
        with pytest.raises(ValueError):
            await subscribe_change_notifications(
                ctx, resource="r", notification_url="",
                expiration_date_time=expiration, client_state="x",
            )
        with pytest.raises(ValueError):
            await subscribe_change_notifications(
                ctx, resource="r", notification_url="https://x",
                expiration_date_time=expiration, client_state="",
            )
        with pytest.raises(ValueError):
            await subscribe_change_notifications(
                ctx, resource="r", notification_url="https://x",
                expiration_date_time=naive, client_state="x",
            )

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# renew_subscription
# =========================================================================


def test_renew_subscription_patches_and_audits(graph_subs_environment) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"ren-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    new_exp = _dt.datetime(2026, 5, 18, 10, 0, 0, tzinfo=_dt.UTC)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.patch(f"{_SUBS_URL}/sub-1").mock(
                return_value=httpx.Response(
                    200,
                    json=_subscription_response(
                        sub_id="sub-1", expiration="2026-05-18T10:00:00Z"
                    ),
                )
            )
            sub = await renew_subscription(
                ctx, "sub-1", expiration_date_time=new_exp
            )
        sent = route.calls.last.request
        body_str = sent.read().decode()
        assert "expirationDateTime" in body_str
        return sub

    sub = _run_with_firm(sm, firm_id, body)
    assert sub.id == "sub-1"
    assert sub.expiration_date_time == new_exp

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.subscriptions.renew"]
    assert len(success) == 1
    assert success[0].actor_type == "system"
    assert success[0].payload["subscription_id"] == "sub-1"


def test_renew_subscription_404_raises_not_found(graph_subs_environment) -> None:
    """Microsoft deleted the subscription (expired beyond grace, etc.)."""
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"ren-404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.patch(f"{_SUBS_URL}/gone").mock(
                return_value=httpx.Response(404, json={"error": "not found"})
            )
            with pytest.raises(ConnectorNotFound):
                await renew_subscription(
                    ctx,
                    "gone",
                    expiration_date_time=_dt.datetime(
                        2026, 5, 18, 10, 0, 0, tzinfo=_dt.UTC
                    ),
                )

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [
        a for a in audits if a.action == "graph.subscriptions.renew_failed"
    ]
    assert len(failed) == 1
    assert failed[0].actor_type == "system"
    assert failed[0].payload["reason"] == "microsoft_404"
    assert failed[0].payload["subscription_id"] == "gone"


def test_renew_subscription_rejects_invalid_inputs(graph_subs_environment) -> None:
    sm = graph_subs_environment["sessionmaker"]
    created = graph_subs_environment["created_firm_ids"]

    firm_id, _ = _seed_and_make_ctx(sm, slug=f"ren-input-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        ctx = AppGraphContext(firm=firm, access_token="tok", session=session)
        with pytest.raises(ValueError):
            await renew_subscription(
                ctx,
                "",
                expiration_date_time=_dt.datetime(
                    2026, 5, 18, 10, 0, 0, tzinfo=_dt.UTC
                ),
            )
        with pytest.raises(ValueError):
            await renew_subscription(
                ctx, "sub-1", expiration_date_time=_dt.datetime(2026, 5, 18)
            )

    _run_with_firm(sm, firm_id, body)
