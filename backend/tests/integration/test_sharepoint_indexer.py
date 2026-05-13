"""Integration tests for ``coworker.memory.sharepoint_indexer``.

The Graph drive API is mocked via respx (each ``/children`` URL).
The KG entity-resolution path runs for real against the test DB
so we exercise the actual pg_trgm match + Document UPSERT.
"""
import uuid

import httpx
import pytest_asyncio
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import Document, Entity, Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.graph.subscriptions import AppGraphContext
from coworker.memory.sharepoint_indexer import index_sharepoint_drive

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def indexer_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )
    created: list[uuid.UUID] = []
    try:
        yield {"sm": sm, "created": created}
    finally:
        for firm_id in created:
            await _cleanup_firm(sm, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = (
        "firms", "users", "audit_log", "token_usage",
        "client_interactions", "lessons", "documents",
        "entity_relationships", "entities", "jobs", "deadlines",
    )
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()
    async with sm() as session:
        try:
            for t in (
                "entity_relationships", "deadlines", "jobs",
                "documents", "lessons", "client_interactions",
                "entities", "audit_log", "token_usage", "users",
            ):
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


async def _seed_firm(sm) -> tuple[uuid.UUID, Firm]:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        firm = Firm(
            id=firm_id,
            name="Indexer Firm",
            slug=f"i-{uuid.uuid4().hex[:8]}",
        )
        session.add(firm)
        await session.commit()
        # Re-read to get a clean attached instance for the test's ctx.
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one()
        # Detach so the test can carry it across sessions.
        session.expunge(firm)
    return firm_id, firm


async def _seed_entity(sm, firm_id, name: str, entity_type: str = "company"):
    async with sm() as session, firm_context(firm_id):
        session.add(
            Entity(
                firm_id=firm_id, entity_type=entity_type, name=name,
            )
        )
        await session.commit()


def _folder(item_id: str, name: str) -> dict:
    return {"id": item_id, "name": name, "folder": {"childCount": 0}}


def _file(item_id: str, name: str, mime: str = "application/pdf") -> dict:
    return {
        "id": item_id,
        "name": name,
        "webUrl": f"https://example.sharepoint.com/sites/x/{item_id}",
        "file": {"mimeType": mime},
    }


def _root_url(drive_id: str = "drv-1") -> str:
    return f"{_GRAPH_ROOT}/drives/{drive_id}/root/children"


def _children_url(drive_id: str, item_id: str) -> str:
    return f"{_GRAPH_ROOT}/drives/{drive_id}/items/{item_id}/children"


def _mock_dispatcher(mapping: dict[str, dict]):
    """Build a respx side_effect that dispatches by exact URL prefix.

    URLs include ``?$top=200`` query strings; respx's regex match
    against the path lets us key by the path-without-query side.
    """

    def _dispatch(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, payload in mapping.items():
            if url.startswith(key):
                return httpx.Response(200, json=payload)
        return httpx.Response(
            418, json={"error": f"unmocked URL: {url}"}
        )

    return _dispatch


async def _run_with_ctx(sm, firm, body):
    """Run an async body with an AppGraphContext bound to the firm."""
    firm_id = firm.id
    async with sm() as session, firm_context(firm_id):
        # Re-attach firm to this session — it was expunged at seed.
        attached = await session.merge(firm)
        ctx = AppGraphContext(
            firm=attached, access_token="bearer-test", session=session,
        )
        return await body(ctx)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_indexer_walks_folders_and_creates_documents(
    indexer_env,
) -> None:
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)
    await _seed_entity(sm, firm_id, "Acme Pty Ltd")

    # Two top-level folders, one matches an entity, one doesn't.
    # Each contains one file.
    mapping = {
        _root_url(): {
            "value": [
                _folder("f-acme", "Acme Pty Ltd"),
                _folder("f-unknown", "Unrelated Folder"),
            ]
        },
        _children_url("drv-1", "f-acme"): {
            "value": [_file("file-1", "BAS-2024.pdf")]
        },
        _children_url("drv-1", "f-unknown"): {
            "value": [_file("file-2", "random.pdf")]
        },
    }

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    stats = await _run_with_ctx(sm, firm, body)

    assert stats.folders_walked == 2
    assert stats.files_indexed == 2
    assert stats.unresolved_folders == ["Unrelated Folder"]
    assert stats.errors == []

    async with sm() as session, firm_context(firm_id):
        docs = (
            await session.execute(
                select(Document).where(Document.firm_id == firm_id)
            )
        ).scalars().all()
        assert len(docs) == 2
        by_title = {d.title: d for d in docs}
        # The file under "Acme Pty Ltd" inherits Acme's entity.
        assert by_title["BAS-2024.pdf"].client_entity_id is not None
        # The file under "Unrelated Folder" has no entity attribution.
        assert by_title["random.pdf"].client_entity_id is None
        # All carry the source identifier and Graph metadata.
        for doc in docs:
            assert doc.source == "sharepoint"
            assert doc.extracted_data["graph_drive_item_id"]
            assert doc.indexed_at is not None


async def test_resync_is_idempotent_and_updates_indexed_at(
    indexer_env,
) -> None:
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)
    await _seed_entity(sm, firm_id, "Acme Pty Ltd")

    mapping = {
        _root_url(): {"value": [_folder("f-acme", "Acme Pty Ltd")]},
        _children_url("drv-1", "f-acme"): {
            "value": [_file("file-1", "doc.pdf")]
        },
    }

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    # First run inserts.
    first = await _run_with_ctx(sm, firm, body)
    assert first.files_indexed == 1

    # Second run should update the same row, not insert a duplicate.
    second = await _run_with_ctx(sm, firm, body)
    assert second.files_indexed == 1

    async with sm() as session, firm_context(firm_id):
        docs = (
            await session.execute(
                select(Document).where(Document.firm_id == firm_id)
            )
        ).scalars().all()
        assert len(docs) == 1


async def test_resync_after_folder_rename_updates_entity_attribution(
    indexer_env,
) -> None:
    """If a Graph item gets moved under a different (resolved) folder,
    its Document's client_entity_id updates accordingly.
    """
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)
    await _seed_entity(sm, firm_id, "Acme Pty Ltd")
    await _seed_entity(sm, firm_id, "Beta Trust", entity_type="trust")

    mapping_v1 = {
        _root_url(): {"value": [_folder("f-acme", "Acme Pty Ltd")]},
        _children_url("drv-1", "f-acme"): {
            "value": [_file("file-1", "doc.pdf")]
        },
    }
    mapping_v2 = {
        _root_url(): {"value": [_folder("f-beta", "Beta Trust")]},
        _children_url("drv-1", "f-beta"): {
            "value": [_file("file-1", "doc.pdf")]  # same file id, different parent
        },
    }

    async def run_v1(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping_v1))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    async def run_v2(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping_v2))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    await _run_with_ctx(sm, firm, run_v1)
    async with sm() as session, firm_context(firm_id):
        doc1 = (
            await session.execute(
                select(Document).where(Document.firm_id == firm_id)
            )
        ).scalar_one()
        acme_id = doc1.client_entity_id

    await _run_with_ctx(sm, firm, run_v2)
    async with sm() as session, firm_context(firm_id):
        doc2 = (
            await session.execute(
                select(Document).where(Document.firm_id == firm_id)
            )
        ).scalar_one()
        beta_id = doc2.client_entity_id

    assert acme_id is not None and beta_id is not None
    assert acme_id != beta_id


async def test_recursion_into_subfolders_inherits_entity(indexer_env) -> None:
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)
    await _seed_entity(sm, firm_id, "Acme Pty Ltd")

    mapping = {
        _root_url(): {"value": [_folder("f-acme", "Acme Pty Ltd")]},
        _children_url("drv-1", "f-acme"): {
            "value": [_folder("f-sub", "2024 BAS")]
        },
        _children_url("drv-1", "f-sub"): {
            "value": [_file("file-1", "Q1.pdf")]
        },
    }

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    stats = await _run_with_ctx(sm, firm, body)
    assert stats.folders_walked == 2  # top + subfolder
    assert stats.files_indexed == 1

    async with sm() as session, firm_context(firm_id):
        doc = (
            await session.execute(
                select(Document).where(Document.firm_id == firm_id)
            )
        ).scalar_one()
        # File in subfolder still inherits the top-level entity.
        assert doc.client_entity_id is not None


async def test_pagination_follows_odata_next_link(indexer_env) -> None:
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)

    page1 = {
        "value": [_folder("f-1", "Folder One")],
        "@odata.nextLink": f"{_GRAPH_ROOT}/drives/drv-1/root/children?token=p2",
    }
    page2 = {"value": [_folder("f-2", "Folder Two")]}

    mapping = {
        # The root URL appears with $top=200; nextLink uses ?token=p2.
        # Order matters because dispatcher matches by startswith.
        _GRAPH_ROOT
        + "/drives/drv-1/root/children?token=p2": page2,
        _root_url(): page1,
        _children_url("drv-1", "f-1"): {"value": []},
        _children_url("drv-1", "f-2"): {"value": []},
    }

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    stats = await _run_with_ctx(sm, firm, body)
    # Both folders walked — page 2 was followed.
    assert stats.folders_walked == 2


async def test_unfollowable_root_records_error_and_returns(
    indexer_env,
) -> None:
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/root/children"
            ).mock(return_value=httpx.Response(404))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    stats = await _run_with_ctx(sm, firm, body)
    assert stats.folders_walked == 0
    assert stats.files_indexed == 0
    assert len(stats.errors) == 1


async def test_non_file_non_folder_items_are_skipped(indexer_env) -> None:
    """OneNote sections / shortcuts / etc. fall into files_skipped."""
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)
    await _seed_entity(sm, firm_id, "Acme Pty Ltd")

    weird_item = {"id": "oddball", "name": "OneNote Section"}  # no file or folder
    mapping = {
        _root_url(): {
            "value": [_folder("f-acme", "Acme Pty Ltd"), weird_item]
        },
        _children_url("drv-1", "f-acme"): {"value": [weird_item]},
    }

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=_mock_dispatcher(mapping))
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    stats = await _run_with_ctx(sm, firm, body)
    assert stats.folders_walked == 1
    assert stats.files_indexed == 0
    assert stats.files_skipped == 2  # weird at root + weird in folder


async def test_per_folder_error_does_not_abort_other_folders(
    indexer_env,
) -> None:
    """One folder failing to list shouldn't prevent the others from indexing."""
    sm = indexer_env["sm"]
    firm_id, firm = await _seed_firm(sm)
    indexer_env["created"].append(firm_id)
    await _seed_entity(sm, firm_id, "Acme Pty Ltd")
    await _seed_entity(sm, firm_id, "Beta Trust", entity_type="trust")

    async def dispatcher(request):
        url = str(request.url)
        if "/items/f-acme/children" in url:
            return httpx.Response(401)  # auth error on one folder
        if "/items/f-beta/children" in url:
            return httpx.Response(
                200, json={"value": [_file("file-1", "deed.pdf")]}
            )
        if "root/children" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        _folder("f-acme", "Acme Pty Ltd"),
                        _folder("f-beta", "Beta Trust"),
                    ]
                },
            )
        return httpx.Response(418)

    async def body(ctx):
        with respx.mock(assert_all_called=False) as rmock:
            rmock.get(
                url__regex=rf"^{_GRAPH_ROOT}/drives/drv-1/(root|items/[^/]+)/children"
            ).mock(side_effect=dispatcher)
            return await index_sharepoint_drive(ctx, drive_id="drv-1")

    stats = await _run_with_ctx(sm, firm, body)
    # Beta succeeded; Acme failed soft.
    assert stats.files_indexed == 1
    assert any("f-acme" in e for e in stats.errors)
