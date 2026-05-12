"""Microsoft Graph drive operations.

Two read entry points so far:

- ``list_drive_items`` — children of a drive root or a folder inside
  it. One page per call; pagination via ``@odata.nextLink`` lands
  later in Phase 3 alongside the SharePoint indexer's delta queries.
- ``download_drive_item`` — streams the binary content of a file into
  a ``SpooledTemporaryFile`` so small files stay in memory and large
  ones spill to disk. Used by Phase 7's vision pipeline (NOA / deed
  PDFs in client folders) and by Phase 4's SharePoint indexer.

Both honour the same rate limiter, audit conventions, and connector
taxonomy as the other Graph endpoints. The download path holds the
per-mailbox semaphore for the full streaming duration, so a 50 MB
download doesn't get interleaved with three other concurrent calls
through the same mailbox's quota.
"""
import datetime as _dt
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from coworker.connectors.exceptions import ConnectorTransient
from coworker.graph.context import GraphContext
from coworker.graph.errors import audit_failure, raise_for_graph_status
from coworker.graph.rate_limit import get_rate_limiter
from coworker.security.audit import append_audit

_GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
_DEFAULT_TOP = 50
_MAX_TOP = 1000  # Graph caps $top at 1000 for /children.

# Tunables for the download path. 10 MB keeps virtually every
# accounting document we care about (NOAs, BAS forms, invoices,
# small deeds) entirely in memory; oversized files (multi-page
# scanned deeds, financial statement bundles) spill to /tmp.
_DEFAULT_MAX_IN_MEMORY = 10 * 1024 * 1024
_DOWNLOAD_CHUNK_SIZE = 64 * 1024
# Downloads can be slow when Microsoft redirects through their CDN
# and the file is large. 5 minutes is generous; the per-mailbox
# semaphore caps concurrent slow downloads at 4.
_DOWNLOAD_TIMEOUT_SECONDS = 300.0

_SELECT_FIELDS = ",".join([
    "id",
    "name",
    "size",
    "createdDateTime",
    "lastModifiedDateTime",
    "webUrl",
    "file",
    "folder",
])

DriveItemType = Literal["file", "folder", "unknown"]


class DriveItem(BaseModel):
    """One drive item — a file, a folder, or something Graph hasn't
    described to us before.

    ``item_type`` is the discriminator: file items carry ``mime_type``
    (from Graph's ``file.mimeType``), folder items carry
    ``child_count`` (from ``folder.childCount``). ``unknown`` is
    forward-compatible — Graph occasionally introduces new item
    kinds (notebook pages, special facets); we surface those rather
    than crash on the read.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    size: int
    item_type: DriveItemType
    mime_type: str | None = None
    child_count: int | None = None
    created_at: _dt.datetime
    modified_at: _dt.datetime
    web_url: str | None = None


@dataclass(frozen=True)
class DriveDownload:
    """Result of a successful ``download_drive_item`` call.

    ``content`` is a ``SpooledTemporaryFile`` positioned at byte 0.
    The caller is responsible for closing it (typically via
    ``with download.content as f:``). On any failure inside
    ``download_drive_item`` — non-2xx response, network error, mid-
    stream disconnect — the spool is closed before the exception
    propagates, so failure paths never leak temp files.

    Frozen so the file reference cannot be replaced after construction.
    Not a Pydantic model because Pydantic doesn't usefully validate
    file objects and serialising a spool to JSON is nonsensical.
    """

    drive_id: str
    item_id: str
    size: int
    content_type: str | None
    content: SpooledTemporaryFile[bytes]


async def list_drive_items(
    ctx: GraphContext,
    drive_id: str,
    item_id: str | None = None,
    *,
    top: int = _DEFAULT_TOP,
) -> list[DriveItem]:
    """List children of a drive's root or a folder inside it.

    Args:
        ctx: per-request Graph bundle. ``ctx.session`` must already be
            inside ``firm_context(ctx.firm.id)``.
        drive_id: target drive (user OneDrive id, or a SharePoint
            document library id resolved via ``/sites/{id}/drives``).
        item_id: when None, lists the drive root; when given, lists
            that item's children. The caller distinguishes "no such
            drive" (``ConnectorNotFound`` with ``drive_id`` extra)
            from "no such folder" (``ConnectorNotFound`` with both
            ``drive_id`` and ``item_id`` extras) via the audit row.
        top: page size, 1 ≤ top ≤ 1000.

    Raises:
        ConnectorAuthError: 401 / 403 / other unhandled 4xx.
        ConnectorNotFound: 404 (drive or folder missing).
        ConnectorRateLimited: 429.
        ConnectorTransient: 5xx, timeout, or network error.
        ValueError: ``drive_id`` is empty, ``item_id`` is an empty
            string (distinguish from ``None``), or ``top`` is outside
            [1, 1000].
    """
    if not drive_id:
        raise ValueError("drive_id must be non-empty")
    if item_id is not None and not item_id:
        raise ValueError("item_id must be non-empty when provided")
    if top < 1 or top > _MAX_TOP:
        raise ValueError(f"top must be between 1 and {_MAX_TOP}, got {top}")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.drive.list_items"
    drive_quoted = quote(drive_id, safe="")

    if item_id is None:
        url = f"{_GRAPH_ROOT}/drives/{drive_quoted}/root/children"
        extra: dict[str, Any] = {"drive_id": drive_id}
    else:
        item_quoted = quote(item_id, safe="")
        url = f"{_GRAPH_ROOT}/drives/{drive_quoted}/items/{item_quoted}/children"
        extra = {"drive_id": drive_id, "item_id": item_id}

    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.get(
                    url,
                    params={"$top": top, "$select": _SELECT_FIELDS},
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                )
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
                extra=extra,
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    await raise_for_graph_status(
        response,
        session=ctx.session,
        firm_id=firm_id_str,
        user_id=user_id_str,
        action=action,
        allow_not_found=True,
        extra=extra,
    )

    body = response.json()
    raw_items = body.get("value", [])
    items = [_parse_drive_item(i) for i in raw_items]

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload={
            "user_id": user_id_str,
            "drive_id": drive_id,
            "item_id": item_id,
            "count": len(items),
            "top": top,
        },
    )
    await ctx.session.commit()

    return items


async def download_drive_item(
    ctx: GraphContext,
    drive_id: str,
    item_id: str,
    *,
    max_in_memory: int = _DEFAULT_MAX_IN_MEMORY,
) -> DriveDownload:
    """Stream the binary content of a drive item into a SpooledTemporaryFile.

    Microsoft Graph's ``/content`` endpoint typically returns a 302
    redirect to a short-lived URL on Microsoft's CDN; httpx follows
    that transparently. The body is streamed in 64 KB chunks and
    written to a ``SpooledTemporaryFile`` whose ``max_in_memory``
    threshold controls when it spills to disk. The default 10 MB
    keeps the common accounting-document workload entirely in RAM.

    Args:
        ctx: per-request Graph bundle.
        drive_id, item_id: target item.
        max_in_memory: byte threshold for the spool. Files at-or-below
            this stay in memory; larger spill to a tempfile.

    Returns:
        ``DriveDownload`` carrying the open spooled file positioned at
        byte 0. The caller closes it.

    Raises:
        ConnectorAuthError: 401 / 403 / other unhandled 4xx.
        ConnectorNotFound: 404 (drive or item missing).
        ConnectorRateLimited: 429.
        ConnectorTransient: 5xx, timeout, or network error.
        ValueError: ``drive_id`` or ``item_id`` empty, or
            ``max_in_memory`` < 1.
    """
    if not drive_id:
        raise ValueError("drive_id must be non-empty")
    if not item_id:
        raise ValueError("item_id must be non-empty")
    if max_in_memory < 1:
        raise ValueError(f"max_in_memory must be >= 1, got {max_in_memory}")

    mailbox_id = str(ctx.user.id)
    firm_id_str = str(ctx.firm.id)
    user_id_str = str(ctx.user.id)
    action = "graph.drive.download_item"
    extra: dict[str, Any] = {"drive_id": drive_id, "item_id": item_id}
    url = (
        f"{_GRAPH_ROOT}/drives/{quote(drive_id, safe='')}"
        f"/items/{quote(item_id, safe='')}/content"
    )

    # Construction is intentionally outside any `with` block: the spool
    # outlives this function (returned to the caller as DriveDownload.content
    # for them to close). The try/except below closes it on every failure
    # path so we never leak. SIM115 doesn't model this lifecycle.
    spooled: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(  # noqa: SIM115
        max_size=max_in_memory
    )
    try:
        size, content_type = await _stream_download(
            ctx=ctx,
            url=url,
            mailbox_id=mailbox_id,
            spooled=spooled,
            firm_id_str=firm_id_str,
            user_id_str=user_id_str,
            action=action,
            extra=extra,
        )
    except BaseException:
        # Includes ConnectorError, asyncio.CancelledError, KeyboardInterrupt,
        # ValueError from a bad-base64 path that doesn't apply here but stays
        # for parity. Anything that propagates out of the streaming block
        # must not leak the spool.
        spooled.close()
        raise

    await append_audit(
        ctx.session,
        firm_id=firm_id_str,
        actor_type="user",
        actor_id=user_id_str,
        action=action,
        payload={
            "user_id": user_id_str,
            "drive_id": drive_id,
            "item_id": item_id,
            "size": size,
            "content_type": content_type,
        },
    )
    await ctx.session.commit()

    return DriveDownload(
        drive_id=drive_id,
        item_id=item_id,
        size=size,
        content_type=content_type,
        content=spooled,
    )


async def _stream_download(
    *,
    ctx: GraphContext,
    url: str,
    mailbox_id: str,
    spooled: SpooledTemporaryFile[bytes],
    firm_id_str: str,
    user_id_str: str,
    action: str,
    extra: dict[str, Any],
) -> tuple[int, str | None]:
    """Acquire the rate-limit slot and stream into ``spooled``.

    Returns ``(size, content_type)`` on success. Raises the same
    connector taxonomy as the rest of this module.
    """
    rate_limiter = get_rate_limiter()
    async with rate_limiter.slot(mailbox_id):
        try:
            async with (
                httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                ) as http,
                http.stream(
                    "GET",
                    url,
                    headers={"Authorization": f"Bearer {ctx.access_token}"},
                ) as response,
            ):
                # raise_for_graph_status only reads status_code and the
                # Retry-After header, both available before the body. So
                # we can dispatch the error path without buffering the
                # (potentially huge) response body.
                await raise_for_graph_status(
                    response,
                    session=ctx.session,
                    firm_id=firm_id_str,
                    user_id=user_id_str,
                    action=action,
                    allow_not_found=True,
                    extra=extra,
                )

                size = 0
                async for chunk in response.aiter_bytes(
                    chunk_size=_DOWNLOAD_CHUNK_SIZE
                ):
                    spooled.write(chunk)
                    size += len(chunk)
                content_type = response.headers.get("Content-Type")
        except httpx.RequestError as exc:
            await audit_failure(
                ctx.session,
                firm_id=firm_id_str,
                user_id=user_id_str,
                action=action,
                reason="network_error",
                extra=extra,
            )
            raise ConnectorTransient(
                "network error talking to Microsoft Graph"
            ) from exc

    spooled.flush()
    spooled.seek(0)
    return size, content_type


def _parse_drive_item(raw: dict[str, Any]) -> DriveItem:
    """Map one Graph drive item dict into a ``DriveItem``."""
    file_block = raw.get("file")
    folder_block = raw.get("folder")

    item_type: DriveItemType
    mime_type: str | None = None
    child_count: int | None = None

    if isinstance(file_block, dict):
        item_type = "file"
        mime_type = file_block.get("mimeType")
    elif isinstance(folder_block, dict):
        item_type = "folder"
        raw_count = folder_block.get("childCount")
        child_count = int(raw_count) if raw_count is not None else None
    else:
        item_type = "unknown"

    return DriveItem(
        id=raw["id"],
        name=raw.get("name") or "",
        size=int(raw.get("size") or 0),
        item_type=item_type,
        mime_type=mime_type,
        child_count=child_count,
        created_at=_parse_graph_datetime(raw["createdDateTime"]),
        modified_at=_parse_graph_datetime(raw["lastModifiedDateTime"]),
        web_url=raw.get("webUrl"),
    )


def _parse_graph_datetime(value: str) -> _dt.datetime:
    """Parse Graph's ``Z``-suffixed ISO-8601 timestamp into a tz-aware datetime."""
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
