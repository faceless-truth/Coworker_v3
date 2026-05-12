"""Integration tests for ``coworker.connectors.fusesign_client``.

Read methods only — write methods land in Phase 3F-2 with their own
tests covering shadow-mode behaviour.
"""
import asyncio
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
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.connectors.fusesign_client import (
    FuseSignClient,
    FuseSignEnvelope,
    FuseSignRecipient,
)
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.security.encryption import encrypt_str

_ENVELOPES_URL = "https://api.fusesign.com/v1/envelopes"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def fusesign_environment(test_database_url):
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
    fusesign_api_key: str | None = "fs-api-key-123",
    shadow_mode: bool = False,
) -> uuid.UUID:
    """Seed a firm with a FuseSign API key. Returns firm_id."""

    async def _run() -> uuid.UUID:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            kwargs: dict = {
                "id": firm_id,
                "name": "FuseSign Test Firm",
                "slug": slug,
                "shadow_mode": shadow_mode,
            }
            if fusesign_api_key is not None:
                kwargs["fusesign_api_key_ciphertext"] = encrypt_str(
                    fusesign_api_key, firm_id=firm_id_str
                )
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


def _run_with_firm(sessionmaker, firm_id, body):
    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            return await body(session, firm)

    return asyncio.run(_run())


def _envelope_payload(
    *,
    eid: str = "env-1",
    name: str = "Engagement Letter — Acme Pty Ltd",
    status: str = "sent",
    created: str = "2026-05-01T10:00:00Z",
    updated: str = "2026-05-02T11:00:00Z",
    recipients: list[dict] | None = None,
    documents: list[dict] | None = None,
) -> dict:
    return {
        "id": eid,
        "name": name,
        "status": status,
        "created_at": created,
        "updated_at": updated,
        "recipients": recipients
        if recipients is not None
        else [
            {
                "id": "r-1",
                "name": "Jane Director",
                "email": "jane@acme.example",
                "role": "signer",
                "status": "pending",
            }
        ],
        "documents": documents if documents is not None else [{"id": "d-1"}],
    }


# =========================================================================
# list_envelopes
# =========================================================================


def test_list_envelopes_returns_parsed_records_and_audits(
    fusesign_environment,
) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-le-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "envelopes": [
            _envelope_payload(eid="env-1", status="sent"),
            _envelope_payload(eid="env-2", status="signed"),
        ]
    }

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_ENVELOPES_URL).mock(
                return_value=httpx.Response(200, json=payload)
            )
            envelopes = await client.list_envelopes()
        sent = route.calls.last.request
        assert sent.headers["X-API-Key"] == "fs-api-key-123"
        assert sent.headers["Accept"] == "application/json"
        return envelopes

    envelopes = _run_with_firm(sm, firm_id, body)
    assert len(envelopes) == 2
    assert all(isinstance(e, FuseSignEnvelope) for e in envelopes)
    assert envelopes[0].id == "env-1"
    assert envelopes[0].status == "sent"
    assert envelopes[0].document_count == 1
    assert envelopes[0].recipients[0].email == "jane@acme.example"
    assert envelopes[0].created_at.tzinfo is not None
    assert envelopes[1].status == "signed"

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "fusesign.envelopes.list"]
    assert len(success) == 1
    assert success[0].payload["count"] == 2


def test_list_envelopes_passes_status_filter(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-les-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(_ENVELOPES_URL).mock(
                return_value=httpx.Response(200, json={"envelopes": []})
            )
            await client.list_envelopes(status="signed")
        assert route.calls.last.request.url.params["status"] == "signed"

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "fusesign.envelopes.list"]
    assert success[0].payload["status"] == "signed"


def test_list_envelopes_handles_data_envelope_variant(
    fusesign_environment,
) -> None:
    """FuseSign sometimes uses {"data": [...]} instead of {"envelopes": [...]}."""
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-led-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_ENVELOPES_URL).mock(
                return_value=httpx.Response(
                    200, json={"data": [_envelope_payload()]}
                )
            )
            return await client.list_envelopes()

    envelopes = _run_with_firm(sm, firm_id, body)
    assert len(envelopes) == 1


def test_list_envelopes_handles_bare_list_variant(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-leb-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_ENVELOPES_URL).mock(
                return_value=httpx.Response(
                    200, json=[_envelope_payload()]
                )
            )
            return await client.list_envelopes()

    envelopes = _run_with_firm(sm, firm_id, body)
    assert len(envelopes) == 1


def test_list_envelopes_401_raises_auth_error_and_audits(
    fusesign_environment,
) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-le401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_ENVELOPES_URL).mock(
                return_value=httpx.Response(401)
            )
            with pytest.raises(ConnectorAuthError):
                await client.list_envelopes()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "fusesign.envelopes.list_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "fusesign_401"


def test_list_envelopes_429_with_retry_after(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-le429-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_ENVELOPES_URL).mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "15"}
                )
            )
            with pytest.raises(ConnectorRateLimited) as excinfo:
                await client.list_envelopes()
            assert excinfo.value.retry_after == 15.0

    _run_with_firm(sm, firm_id, body)


def test_list_envelopes_5xx_raises_transient(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-le5xx-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_ENVELOPES_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(ConnectorTransient):
                await client.list_envelopes()

    _run_with_firm(sm, firm_id, body)


def test_list_envelopes_network_error_audits_and_raises_transient(
    fusesign_environment,
) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-lenet-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(_ENVELOPES_URL).mock(
                side_effect=httpx.ConnectError("no net")
            )
            with pytest.raises(ConnectorTransient):
                await client.list_envelopes()

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "fusesign.envelopes.list_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "network_error"


def test_list_envelopes_missing_api_key_raises_auth_error(
    fusesign_environment,
) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(
        sm,
        slug=f"fs-nokey-{uuid.uuid4().hex[:8]}",
        fusesign_api_key=None,
    )
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with pytest.raises(ConnectorAuthError, match="fusesign_api_key"):
            await client.list_envelopes()

    _run_with_firm(sm, firm_id, body)


def test_list_envelopes_rejects_empty_status(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-lees-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.list_envelopes(status="")

    _run_with_firm(sm, firm_id, body)


# =========================================================================
# get_envelope
# =========================================================================


def test_get_envelope_returns_parsed_record_and_audits(
    fusesign_environment,
) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-ge-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_ENVELOPES_URL}/env-99").mock(
                return_value=httpx.Response(
                    200, json={"envelope": _envelope_payload(eid="env-99")}
                )
            )
            return await client.get_envelope("env-99")

    env = _run_with_firm(sm, firm_id, body)
    assert isinstance(env, FuseSignEnvelope)
    assert env.id == "env-99"
    assert isinstance(env.recipients[0], FuseSignRecipient)

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "fusesign.envelopes.get"]
    assert len(success) == 1
    assert success[0].payload["envelope_id"] == "env-99"


def test_get_envelope_bare_object_variant(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-geb-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_ENVELOPES_URL}/env-1").mock(
                return_value=httpx.Response(
                    200, json=_envelope_payload(eid="env-1")
                )
            )
            return await client.get_envelope("env-1")

    env = _run_with_firm(sm, firm_id, body)
    assert env.id == "env-1"


def test_get_envelope_404_raises_not_found(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-ge404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_ENVELOPES_URL}/missing").mock(
                return_value=httpx.Response(404)
            )
            with pytest.raises(ConnectorNotFound):
                await client.get_envelope("missing")

    _run_with_firm(sm, firm_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "fusesign.envelopes.get_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "fusesign_404"
    assert failed[0].payload["envelope_id"] == "missing"


def test_get_envelope_percent_encodes_id(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-geurl-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    eid = "env/with=slashes"

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(
                url__regex=(
                    r"^https://api\.fusesign\.com/v1/envelopes/[^/]+$"
                )
            ).mock(
                return_value=httpx.Response(
                    200, json=_envelope_payload(eid=eid)
                )
            )
            await client.get_envelope(eid)
        sent_url = str(route.calls.last.request.url)
        assert "with/slashes" not in sent_url
        assert "%2F" in sent_url
        assert "%3D" in sent_url

    _run_with_firm(sm, firm_id, body)


def test_get_envelope_rejects_empty_id(fusesign_environment) -> None:
    sm = fusesign_environment["sessionmaker"]
    created = fusesign_environment["created_firm_ids"]

    firm_id = _seed_firm(sm, slug=f"fs-gee-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(session, firm):
        client = FuseSignClient(firm, session=session)
        with pytest.raises(ValueError):
            await client.get_envelope("")

    _run_with_firm(sm, firm_id, body)
