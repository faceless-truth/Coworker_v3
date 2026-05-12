"""Integration tests for ``coworker.graph.drive``.

Same pattern as ``test_graph_mail.py`` / ``test_graph_calendar.py``:
direct call into the helper under firm_context, Microsoft Graph
mocked via respx, real DB.
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
    ConnectorNotFound,
    ConnectorTransient,
)
from coworker.db.models.audit import AuditLogEntry
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.context import GraphContext
from coworker.graph.drive import (
    DriveDownload,
    DriveItem,
    download_drive_item,
    list_drive_items,
)
from coworker.security.encryption import encrypt_str

_GRAPH_DRIVES_BASE = "https://graph.microsoft.com/v1.0/drives"


# --------------------------- fixtures / helpers -----------------------------


@pytest.fixture
def graph_drive_environment(test_database_url):
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
                text("DELETE FROM users WHERE firm_id = :id"), {"id": str(firm_id)}
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


def _seed(sessionmaker, *, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    async def _run() -> tuple[uuid.UUID, uuid.UUID]:
        firm_id = uuid.uuid4()
        firm_id_str = str(firm_id)
        async with sessionmaker() as session, firm_context(firm_id):
            session.add(
                Firm(
                    id=firm_id,
                    name="Drive Test Firm",
                    slug=slug,
                    azure_tenant_id=str(uuid.uuid4()),
                    azure_client_id=str(uuid.uuid4()),
                    azure_client_secret_ciphertext=encrypt_str(
                        "secret", firm_id=firm_id_str
                    ),
                )
            )
            await session.flush()
            user = User(
                firm_id=firm_id,
                azure_object_id=uuid.uuid4().hex,
                upn=f"drv-{uuid.uuid4().hex[:8]}@example.com",
                display_name="Drive Test User",
                ms_access_token_ciphertext=encrypt_str(
                    "test-access", firm_id=firm_id_str
                ),
                ms_refresh_token_ciphertext=encrypt_str(
                    "test-refresh", firm_id=firm_id_str
                ),
                ms_token_expires_at=_dt.datetime.now(_dt.UTC)
                + _dt.timedelta(hours=1),
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            await session.commit()
            return firm_id, user_id

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


def _run_with_ctx(sessionmaker, firm_id, user_id, body):
    async def _run():
        async with sessionmaker() as session, firm_context(firm_id):
            firm = (
                await session.execute(select(Firm).where(Firm.id == firm_id))
            ).scalar_one()
            user = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            ctx = GraphContext(
                firm=firm, user=user, access_token="bearer-xyz", session=session
            )
            return await body(ctx)

    return asyncio.run(_run())


def _file_item(
    *,
    item_id: str = "file-1",
    name: str = "tax_return.pdf",
    size: int = 12345,
    mime_type: str = "application/pdf",
) -> dict:
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "createdDateTime": "2026-05-01T10:00:00Z",
        "lastModifiedDateTime": "2026-05-02T11:00:00Z",
        "webUrl": "https://example.sharepoint.com/...",
        "file": {"mimeType": mime_type, "hashes": {"sha256Hash": "abc"}},
    }


def _folder_item(
    *,
    item_id: str = "folder-1",
    name: str = "Clients",
    child_count: int = 3,
) -> dict:
    return {
        "id": item_id,
        "name": name,
        "size": 0,
        "createdDateTime": "2026-05-01T10:00:00Z",
        "lastModifiedDateTime": "2026-05-02T11:00:00Z",
        "webUrl": "https://example.sharepoint.com/folder/...",
        "folder": {"childCount": child_count},
    }


# =========================================================================
# list_drive_items
# =========================================================================


def test_list_drive_items_root_returns_parsed_and_audits(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-root-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    drive_id = "b!drive-abc"
    payload = {
        "value": [
            _file_item(item_id="f-1", name="NOA-2025.pdf"),
            _folder_item(item_id="d-1", name="Clients", child_count=42),
        ]
    }

    async def body(ctx: GraphContext) -> list[DriveItem]:
        url = f"{_GRAPH_DRIVES_BASE}/{drive_id}/root/children"
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(url).mock(
                return_value=httpx.Response(200, json=payload)
            )
            items = await list_drive_items(ctx, drive_id, top=25)
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer bearer-xyz"
        assert sent.url.params["$top"] == "25"
        return items

    items = _run_with_ctx(sm, firm_id, user_id, body)

    assert len(items) == 2
    file_item, folder_item = items
    assert file_item.item_type == "file"
    assert file_item.mime_type == "application/pdf"
    assert file_item.child_count is None
    assert folder_item.item_type == "folder"
    assert folder_item.child_count == 42
    assert folder_item.mime_type is None
    assert file_item.created_at.tzinfo is not None

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.drive.list_items"]
    assert len(success) == 1
    assert success[0].payload["drive_id"] == drive_id
    assert success[0].payload["item_id"] is None
    assert success[0].payload["count"] == 2


def test_list_drive_items_folder_hits_items_endpoint(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-folder-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    drive_id = "b!drive-abc"
    item_id = "01ABCD-folder"

    async def body(ctx: GraphContext) -> list[DriveItem]:
        url = f"{_GRAPH_DRIVES_BASE}/{drive_id}/items/{item_id}/children"
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(url).mock(
                return_value=httpx.Response(
                    200, json={"value": [_file_item(item_id="f-1")]}
                )
            )
            items = await list_drive_items(ctx, drive_id, item_id)
        # endpoint is items/{item_id}/children, not root/children
        assert "/items/" in str(route.calls.last.request.url)
        assert "/root/children" not in str(route.calls.last.request.url)
        return items

    items = _run_with_ctx(sm, firm_id, user_id, body)
    assert len(items) == 1

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.drive.list_items"]
    assert success[0].payload["item_id"] == item_id


def test_list_drive_items_handles_unknown_item_type(
    graph_drive_environment,
) -> None:
    """Items with neither `file` nor `folder` facets surface as 'unknown'."""
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-unknown-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = {
        "value": [
            {
                "id": "x-1",
                "name": "OneNote section",
                "size": 0,
                "createdDateTime": "2026-05-01T10:00:00Z",
                "lastModifiedDateTime": "2026-05-02T11:00:00Z",
            }
        ]
    }

    async def body(ctx: GraphContext) -> list[DriveItem]:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/root/children").mock(
                return_value=httpx.Response(200, json=payload)
            )
            return await list_drive_items(ctx, "d1")

    items = _run_with_ctx(sm, firm_id, user_id, body)
    assert items[0].item_type == "unknown"
    assert items[0].mime_type is None
    assert items[0].child_count is None


def test_list_drive_items_percent_encodes_special_chars(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-encode-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    drive_id = "drive/with=slashes"
    item_id = "item=plus/slash"

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            route = rmock.get(
                url__regex=(
                    r"^https://graph\.microsoft\.com/v1\.0/drives/[^/]+/items/[^/]+/children\b"
                )
            ).mock(return_value=httpx.Response(200, json={"value": []}))
            await list_drive_items(ctx, drive_id, item_id)
        sent_url = str(route.calls.last.request.url)
        assert "%2F" in sent_url
        assert "%3D" in sent_url
        assert "/with=slashes/" not in sent_url

    _run_with_ctx(sm, firm_id, user_id, body)


def test_list_drive_items_404_raises_not_found_and_audits(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/missing/root/children").mock(
                return_value=httpx.Response(404, json={"error": "not found"})
            )
            with pytest.raises(ConnectorNotFound):
                await list_drive_items(ctx, "missing")

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.drive.list_items_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_404"
    assert failed[0].payload["drive_id"] == "missing"


def test_list_drive_items_401_raises_auth_error(graph_drive_environment) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/root/children").mock(
                return_value=httpx.Response(401)
            )
            with pytest.raises(ConnectorAuthError):
                await list_drive_items(ctx, "d1")

    _run_with_ctx(sm, firm_id, user_id, body)


def test_list_drive_items_network_error_raises_transient_and_audits(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/root/children").mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await list_drive_items(ctx, "d1")

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.drive.list_items_failed"]
    assert failed[0].payload["reason"] == "network_error"
    assert failed[0].payload["drive_id"] == "d1"


def test_list_drive_items_rejects_invalid_inputs(graph_drive_environment) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"drv-input-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await list_drive_items(ctx, "")
        with pytest.raises(ValueError):
            await list_drive_items(ctx, "d1", "")
        with pytest.raises(ValueError):
            await list_drive_items(ctx, "d1", top=0)
        with pytest.raises(ValueError):
            await list_drive_items(ctx, "d1", top=1001)

    _run_with_ctx(sm, firm_id, user_id, body)


# =========================================================================
# download_drive_item
# =========================================================================


def test_download_drive_item_returns_full_content_and_audits(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"dl-ok-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    payload = b"%PDF-1.4\nfake pdf body"

    async def body(ctx: GraphContext) -> DriveDownload:
        url = f"{_GRAPH_DRIVES_BASE}/d1/items/f1/content"
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(url).mock(
                return_value=httpx.Response(
                    200,
                    content=payload,
                    headers={"Content-Type": "application/pdf"},
                )
            )
            return await download_drive_item(ctx, "d1", "f1")

    download = _run_with_ctx(sm, firm_id, user_id, body)
    try:
        assert isinstance(download, DriveDownload)
        assert download.size == len(payload)
        assert download.content_type == "application/pdf"
        assert download.drive_id == "d1"
        assert download.item_id == "f1"
        data = download.content.read()
        assert data == payload
    finally:
        download.content.close()

    audits = _audit_entries(sm, firm_id)
    success = [a for a in audits if a.action == "graph.drive.download_item"]
    assert len(success) == 1
    assert success[0].payload["size"] == len(payload)
    assert success[0].payload["content_type"] == "application/pdf"


def test_download_drive_item_streams_multi_chunk_payload(
    graph_drive_environment,
) -> None:
    """Body larger than one chunk round-trips identically.

    Uses ~200 KB to ensure aiter_bytes(chunk_size=64 KB) iterates
    multiple times. The exact chunk boundaries depend on httpx /
    respx internals; the contract we care about is byte-for-byte
    fidelity end-to-end.
    """
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"dl-multi-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    # Deterministic payload — repeat a 16-byte pattern to make any
    # truncation / reorder obvious if it ever broke.
    pattern = b"0123456789ABCDEF"
    payload = pattern * 13_000  # 208,000 bytes — comfortably over one chunk

    async def body(ctx: GraphContext) -> DriveDownload:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/items/big/content").mock(
                return_value=httpx.Response(
                    200,
                    content=payload,
                    headers={"Content-Type": "application/octet-stream"},
                )
            )
            return await download_drive_item(ctx, "d1", "big")

    download = _run_with_ctx(sm, firm_id, user_id, body)
    try:
        assert download.size == len(payload)
        assert download.content.read() == payload
    finally:
        download.content.close()


def test_download_drive_item_404_raises_not_found_and_audits(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"dl-404-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/items/missing/content").mock(
                return_value=httpx.Response(404, json={"error": "not found"})
            )
            with pytest.raises(ConnectorNotFound):
                await download_drive_item(ctx, "d1", "missing")

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.drive.download_item_failed"]
    assert len(failed) == 1
    assert failed[0].payload["reason"] == "microsoft_404"
    assert failed[0].payload["drive_id"] == "d1"
    assert failed[0].payload["item_id"] == "missing"


def test_download_drive_item_401_raises_auth_error(graph_drive_environment) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"dl-401-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/items/f1/content").mock(
                return_value=httpx.Response(401)
            )
            with pytest.raises(ConnectorAuthError):
                await download_drive_item(ctx, "d1", "f1")

    _run_with_ctx(sm, firm_id, user_id, body)


def test_download_drive_item_network_error_raises_transient_and_audits(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"dl-net-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with respx.mock(assert_all_called=True) as rmock:
            rmock.get(f"{_GRAPH_DRIVES_BASE}/d1/items/f1/content").mock(
                side_effect=httpx.ConnectError("no network")
            )
            with pytest.raises(ConnectorTransient):
                await download_drive_item(ctx, "d1", "f1")

    _run_with_ctx(sm, firm_id, user_id, body)

    audits = _audit_entries(sm, firm_id)
    failed = [a for a in audits if a.action == "graph.drive.download_item_failed"]
    assert failed[0].payload["reason"] == "network_error"
    assert failed[0].payload["drive_id"] == "d1"


def test_download_drive_item_rejects_invalid_inputs(
    graph_drive_environment,
) -> None:
    sm = graph_drive_environment["sessionmaker"]
    created = graph_drive_environment["created_firm_ids"]

    firm_id, user_id = _seed(sm, slug=f"dl-input-{uuid.uuid4().hex[:8]}")
    created.append(firm_id)

    async def body(ctx: GraphContext) -> None:
        with pytest.raises(ValueError):
            await download_drive_item(ctx, "", "f1")
        with pytest.raises(ValueError):
            await download_drive_item(ctx, "d1", "")
        with pytest.raises(ValueError):
            await download_drive_item(ctx, "d1", "f1", max_in_memory=0)

    _run_with_ctx(sm, firm_id, user_id, body)
