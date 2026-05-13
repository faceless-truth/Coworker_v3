"""SharePoint indexer — walks a firm's Clients drive and persists Document rows.

Phase 4E ships the **structural** indexer: walk the folder tree
under a configured Clients root, resolve each top-level folder
name to a KG entity via ``sharepoint_resolver``, and persist one
``Document`` row per file with the metadata the Phase 7 vision
pipeline will need.

What this commit explicitly does NOT do:

- **Body extraction / OCR / classification** — those are Phase 7
  (vision pipeline). 4E records ``extracted_data.graph_drive_item_id``
  + title + parent entity; Phase 7 reads ``Document`` rows that need
  enrichment and fills in ``body``, ``doc_type``, ``content_hash``,
  ``embedding``, ``summary``.
- **Spaces upload** — files stay in SharePoint; ``spaces_url``
  remains NULL until Phase 7 copies them.
- **Graph delta queries** — full enumeration each run. A delta-
  query version is a Phase 4E follow-up once a real tenant
  experiences the cost of a full walk on a large drive.

App-only auth
-------------

The indexer is a background job that runs without a signed-in
user, so it uses ``AppGraphContext`` rather than the user-scoped
``GraphContext`` the rest of the Graph code uses. The per-request
``list_drive_items`` from ``coworker.graph.drive`` expects
``GraphContext`` (rate-limited by mailbox_id); to avoid retrofitting
that path with optional user fields, this module makes its own
Graph drive call against the app-only token. The audit rows use
``actor_type="system"``.

Recursion
---------

The walk recurses depth-first with a depth limit
(``_MAX_RECURSION_DEPTH``) so a misconfigured SharePoint tree or a
folder cycle (rare but possible via shortcuts) doesn't run forever.
Each visited folder counts toward the limit including the root.

Idempotency
-----------

Re-running the indexer over the same content is safe: the
file-level UPSERT looks up an existing Document by
``extracted_data->>'graph_drive_item_id' = :graph_id`` within the
firm and updates ``indexed_at`` rather than inserting a duplicate.
A folder rename that changes the resolved entity_id will update
the Document's ``client_entity_id`` on the next run.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select, text

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorNotFound,
    ConnectorRateLimited,
    ConnectorTransient,
)
from coworker.db.models import Document
from coworker.graph.subscriptions import AppGraphContext
from coworker.knowledge_graph.sharepoint_resolver import (
    resolve_folder_to_entity,
)

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_DEFAULT_TOP = 200
_MAX_RECURSION_DEPTH = 20


@dataclass
class IndexStats:
    """Result of one ``index_sharepoint_drive`` run.

    ``files_indexed`` covers both inserts and updates collapsed —
    every file the indexer touched. ``files_skipped`` counts items
    the walker hit that are not files (folders are counted in
    ``folders_walked``; unknown drive-item types fall into
    ``files_skipped``). ``unresolved_folders`` are the top-level
    folder names that didn't match any entity above the resolver's
    threshold; the principal can map these manually via the Phase 13
    onboarding wizard or a future admin UI.
    """

    folders_walked: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    unresolved_folders: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def index_sharepoint_drive(
    ctx: AppGraphContext,
    *,
    drive_id: str,
    root_folder_id: str | None = None,
) -> IndexStats:
    """Walk the drive's Clients root and persist Document rows.

    Args:
        ctx: app-only Graph context. Session must already be inside
            ``firm_context(ctx.firm.id)``.
        drive_id: target drive (typically the firm's SharePoint
            document library id, resolved during onboarding and
            stored on the firm row).
        root_folder_id: the Clients-root folder item id, or None
            to start at the drive's actual root. The firm's
            ``sharepoint_clients_folder_path`` is resolved to an
            item id elsewhere (Phase 13 wizard) and passed in here.

    Returns:
        ``IndexStats``. Errors are recorded as soft entries — the
        walker doesn't abort on a per-folder Graph failure.
    """
    stats = IndexStats()

    try:
        top_level = await _list_children(
            ctx, drive_id=drive_id, item_id=root_folder_id
        )
    except (ConnectorAuthError, ConnectorNotFound) as exc:
        stats.errors.append(f"list root: {exc}")
        return stats

    for top_item in top_level:
        if top_item.get("folder") is None:
            stats.files_skipped += 1
            continue
        stats.folders_walked += 1
        folder_name = top_item.get("name") or ""
        match = await resolve_folder_to_entity(
            ctx.session, folder_name=folder_name
        )
        entity_id: uuid.UUID | None = (
            match.entity_id if match is not None else None
        )
        if match is None:
            stats.unresolved_folders.append(folder_name)

        await _walk_folder(
            ctx,
            drive_id=drive_id,
            item_id=top_item["id"],
            entity_id=entity_id,
            depth=1,
            stats=stats,
        )

    await ctx.session.commit()
    return stats


async def _walk_folder(
    ctx: AppGraphContext,
    *,
    drive_id: str,
    item_id: str,
    entity_id: uuid.UUID | None,
    depth: int,
    stats: IndexStats,
) -> None:
    """Recurse into a folder; index files; recurse into subfolders.

    Files inherit the entity_id resolved at the top level — the
    real-world SharePoint structure under MC&S has each top-level
    client folder containing year/topic subfolders, so a single
    folder→entity match propagates cleanly down the tree.
    """
    if depth > _MAX_RECURSION_DEPTH:
        stats.errors.append(
            f"max recursion depth ({_MAX_RECURSION_DEPTH}) exceeded "
            f"at item {item_id}"
        )
        return

    try:
        children = await _list_children(
            ctx, drive_id=drive_id, item_id=item_id
        )
    except (ConnectorAuthError, ConnectorNotFound) as exc:
        stats.errors.append(f"list children {item_id}: {exc}")
        return

    for child in children:
        if child.get("folder") is not None:
            stats.folders_walked += 1
            await _walk_folder(
                ctx,
                drive_id=drive_id,
                item_id=child["id"],
                entity_id=entity_id,
                depth=depth + 1,
                stats=stats,
            )
            continue
        if child.get("file") is None:
            stats.files_skipped += 1
            continue

        try:
            await _upsert_document(
                ctx, child=child, entity_id=entity_id,
            )
        except Exception as exc:
            stats.errors.append(
                f"upsert document {child.get('id')}: {exc}"
            )
            continue
        stats.files_indexed += 1


async def _upsert_document(
    ctx: AppGraphContext,
    *,
    child: dict[str, Any],
    entity_id: uuid.UUID | None,
) -> None:
    """Find-or-update the Document row for a Graph file.

    Identity key: ``extracted_data->>'graph_drive_item_id'``. The
    Document model doesn't have a dedicated column for this — using
    JSONB keeps the schema stable while Phase 7 figures out the
    right structural columns (content_hash will likely become the
    canonical key once vision computes it).
    """
    graph_id = child["id"]
    graph_web_url = child.get("webUrl") or ""
    title = child.get("name") or ""

    file_block = child.get("file") or {}
    mime_type = file_block.get("mimeType")

    existing_id = await ctx.session.execute(
        text(
            """
            SELECT id FROM documents
            WHERE firm_id = :firm
              AND extracted_data->>'graph_drive_item_id' = :graph_id
            LIMIT 1
            """
        ),
        {"firm": str(ctx.firm.id), "graph_id": graph_id},
    )
    existing_row = existing_id.scalar_one_or_none()
    now = _dt.datetime.now(_dt.UTC)

    extracted_data = {
        "graph_drive_item_id": graph_id,
        "graph_web_url": graph_web_url,
        "graph_mime_type": mime_type,
        "indexer_version": 1,
    }

    if existing_row is None:
        ctx.session.add(
            Document(
                firm_id=ctx.firm.id,
                source="sharepoint",
                doc_type=None,  # Phase 7 vision fills this in
                client_entity_id=entity_id,
                title=title,
                summary=None,
                body=None,
                spaces_url=None,
                content_hash=None,
                extracted_data=extracted_data,
                indexed_at=now,
            )
        )
        await ctx.session.flush()
        return

    # Update path — refresh title / entity_id / indexed_at; preserve
    # whatever Phase 7 has already extracted.
    doc = (
        await ctx.session.execute(
            select(Document).where(Document.id == existing_row)
        )
    ).scalar_one()
    doc.title = title
    doc.client_entity_id = entity_id
    doc.indexed_at = now
    # Merge into existing extracted_data so vision-added keys persist.
    merged = dict(doc.extracted_data or {})
    merged.update(extracted_data)
    doc.extracted_data = merged
    await ctx.session.flush()


async def _list_children(
    ctx: AppGraphContext,
    *,
    drive_id: str,
    item_id: str | None,
) -> list[dict[str, Any]]:
    """List a drive item's children via app-only Graph auth.

    Lives here rather than reusing ``coworker.graph.drive.list_drive_items``
    because that helper takes a user-scoped ``GraphContext`` and uses
    a mailbox-keyed rate limiter that doesn't fit a system-actor
    background job. The returned shape is the raw Graph JSON for
    each child — the indexer is the only consumer, so the typed
    ``DriveItem`` model from ``graph/drive.py`` isn't worth pulling
    in here.

    Pagination: follows ``@odata.nextLink`` through the full
    response. ``$top=200`` per page caps per-call cost.

    Raises:
        ConnectorAuthError: 401 / 403 / other unhandled 4xx.
        ConnectorNotFound: 404 (folder deleted between runs).
        ConnectorRateLimited: 429.
        ConnectorTransient: 5xx / network error.
    """
    if item_id is None:
        url: str | None = (
            f"{_GRAPH_ROOT}/drives/{quote(drive_id, safe='')}/root/children"
            f"?$top={_DEFAULT_TOP}"
        )
    else:
        url = (
            f"{_GRAPH_ROOT}/drives/{quote(drive_id, safe='')}"
            f"/items/{quote(item_id, safe='')}/children"
            f"?$top={_DEFAULT_TOP}"
        )

    all_items: list[dict[str, Any]] = []
    while url is not None:
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    url,
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

        status = response.status_code
        if status == 404:
            raise ConnectorNotFound(
                f"Graph returned 404 listing drive {drive_id} item {item_id}"
            )
        if status == 401 or status == 403:
            raise ConnectorAuthError(
                f"Graph rejected drive list: HTTP {status}"
            )
        if status == 429:
            retry_after_raw = response.headers.get("Retry-After")
            try:
                retry_after = (
                    float(retry_after_raw) if retry_after_raw else None
                )
            except (TypeError, ValueError):
                retry_after = None
            raise ConnectorRateLimited(retry_after=retry_after)
        if 500 <= status < 600:
            raise ConnectorTransient(f"Graph returned {status}")
        if status >= 400:
            raise ConnectorAuthError(f"Graph returned {status}")

        body = response.json()
        all_items.extend(body.get("value") or [])
        url = body.get("@odata.nextLink")
    return all_items
