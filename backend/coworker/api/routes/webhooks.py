"""Microsoft Graph webhook receiver.

Two responsibilities:

1. **Subscription handshake** — when a new subscription is created
   via ``graph.subscriptions.subscribe_change_notifications``,
   Microsoft sends a POST with ``?validationToken=...`` and
   expects a 200 response with the plain-text token within 10
   seconds. We answer immediately and don't touch the queue.

2. **Notification dispatch** — Microsoft POSTs a JSON body with a
   ``value`` array of notifications. For each, we extract the
   triggering resource (typically a message id) and enqueue a
   ``PluginEvent`` to Redis. A separate worker pool fans out to
   the plugins listening to this trigger; the webhook stays
   intentionally thin so we always return 202 quickly (Microsoft
   times out at 30s and will throttle if we're slow).

Security
--------

For the thin-slice receiver we resolve firms via slug-in-URL. The
``clientState`` carried in the notification body should match the
stored per-subscription state — that lookup waits for the
Phase 11 ``subscriptions`` table. Until then this endpoint
**accepts any well-formed notification for a known firm**, which
is fine while the URL is unpublished but is a security gap we
must close before going public. Tracked as a phase 11 carry-
forward.

Even without clientState validation, the endpoint refuses to
enqueue for unknown firm slugs (returns 202 anyway so a probe
can't enumerate firms).
"""
import datetime as _dt
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.db import redis as redis_module
from coworker.db.firms import lookup_firm_by_slug
from coworker.db.session import get_session
from coworker.workers.plugin_queue import PluginEventQueue

router = APIRouter()


@router.post("/webhooks/graph/{firm_slug}")
async def graph_webhook(
    firm_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Receive a Graph change notification.

    Microsoft's contract:

    - For the handshake, the POST has ``?validationToken=...``
      query param and an empty body. The response body must be
      the plain-text token within 10s.
    - For a real notification, the body is JSON with a ``value``
      array of notification objects.

    We respond 202 to real notifications regardless of whether
    we enqueue anything — Microsoft retries on non-202, and a
    failure to enqueue (firm not found, malformed payload) is
    our problem to surface via logs and audit, not theirs to
    retry.
    """
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        logger.info(
            "graph webhook handshake firm_slug={} token={}",
            firm_slug,
            validation_token[:8] + "..." if len(validation_token) > 8
            else validation_token,
        )
        return PlainTextResponse(validation_token, status_code=200)

    try:
        body = await request.json()
    except ValueError:
        logger.warning(
            "graph webhook bad json firm_slug={}", firm_slug,
        )
        return Response(status_code=202)

    if not isinstance(body, dict):
        return Response(status_code=202)

    notifications = body.get("value") or []
    if not isinstance(notifications, list) or not notifications:
        return Response(status_code=202)

    firm = await lookup_firm_by_slug(session, firm_slug)
    if firm is None:
        # Don't leak whether a slug exists — log it but still 202.
        logger.warning(
            "graph webhook unknown firm_slug={}", firm_slug,
        )
        return Response(status_code=202)

    # Call through the module so test monkeypatching of get_redis
    # takes effect. ``from coworker.db.redis import get_redis``
    # would bind the original at import time and bypass the patch.
    queue = PluginEventQueue(redis_module.get_redis())

    enqueued = 0
    for notif in notifications:
        if not isinstance(notif, dict):
            continue
        event_data = _build_event_data(notif)
        if event_data is None:
            continue
        await queue.enqueue(
            trigger="email_received",
            firm_slug=firm_slug,
            firm_id=firm.id,
            event_data=event_data,
        )
        enqueued += 1

    if enqueued:
        logger.info(
            "graph webhook enqueued count={} firm_slug={}",
            enqueued,
            firm_slug,
        )

    return Response(status_code=202)


def _build_event_data(notification: dict[str, Any]) -> dict[str, Any] | None:
    """Build the PluginEvent event_data payload from one notification.

    Microsoft's notification shape:

        {
          "subscriptionId": "...",
          "clientState": "...",
          "changeType": "created" | "updated" | ...,
          "resource": "users/{userId}/messages/{messageId}",
          "resourceData": {
            "@odata.type": "#Microsoft.Graph.Message",
            "@odata.id": "...",
            "@odata.etag": "...",
            "id": "<message_id>"
          },
          ...
        }

    Returns None when the notification doesn't carry enough to
    construct a meaningful event (missing message id, unsupported
    resource type) — the receiver still returns 202 and logs.
    """
    resource_data = notification.get("resourceData")
    if not isinstance(resource_data, dict):
        return None
    message_id = resource_data.get("id")
    if not message_id:
        return None
    change_type = notification.get("changeType") or "created"
    return {
        "message_id": message_id,
        "change_type": change_type,
        "subscription_id": notification.get("subscriptionId"),
        "resource": notification.get("resource"),
        "received_at_webhook": _dt.datetime.now(_dt.UTC).isoformat(),
    }
