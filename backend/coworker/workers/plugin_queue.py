"""Redis-backed queue for plugin events.

The webhook receiver pushes events; the worker pool pops them.
Single queue for now — Phase 11's split-queue design (graph_events
for raw notifications + plugin_runs for fanned-out per-plugin
work) lands when we need per-plugin retry independence.

Queue layout::

    LPUSH queue:plugin_events <json>
    BRPOP queue:plugin_events 0      # blocking pop

JSON payload shape::

    {
      "event_id": "<uuid>",
      "trigger": "email_received" | "fusesign_event" | "scheduled" | ...,
      "firm_slug": "<slug>",
      "firm_id": "<uuid>",
      "event_data": {
        # trigger-specific. For email_received:
        #   "message_id": "...", "from": "...", "subject": "...", "body_preview": "..."
        # For scheduled:
        #   "schedule_cron": "0 6 * * *", "scheduled_at": "<iso>"
      },
      "enqueued_at": "<iso>"
    }
"""
import datetime as _dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

_QUEUE_KEY = "queue:plugin_events"


@dataclass(frozen=True)
class PluginEvent:
    """A queued event waiting to be fanned out to one or more plugins.

    ``firm_id`` is the canonical reference; ``firm_slug`` is carried
    redundantly so the worker can log the firm before looking up
    the row. ``trigger`` matches the plugin base's ``Trigger``
    literal; downstream code uses it to filter the plugin registry.
    """

    event_id: uuid.UUID
    trigger: str
    firm_slug: str
    firm_id: uuid.UUID
    event_data: dict[str, Any]
    enqueued_at: _dt.datetime


class PluginEventQueue:
    """Thin async wrapper around the Redis list.

    Constructed once per worker process. Stateless beyond the Redis
    client. The instance can sit behind any number of concurrent
    enqueuers / dequeuers because Redis's LPUSH/BRPOP are atomic.
    """

    def __init__(self, redis: Redis, *, queue_key: str = _QUEUE_KEY) -> None:
        self._redis = redis
        self._queue_key = queue_key

    async def enqueue(
        self,
        *,
        trigger: str,
        firm_slug: str,
        firm_id: uuid.UUID,
        event_data: dict[str, Any],
    ) -> PluginEvent:
        """Append an event to the queue. Returns the constructed PluginEvent."""
        event = PluginEvent(
            event_id=uuid.uuid4(),
            trigger=trigger,
            firm_slug=firm_slug,
            firm_id=firm_id,
            event_data=event_data,
            enqueued_at=_dt.datetime.now(_dt.UTC),
        )
        await self._redis.lpush(self._queue_key, _encode(event))  # type: ignore[misc]
        return event

    async def dequeue(self, *, timeout_seconds: float = 0) -> PluginEvent | None:
        """Pop the next event, blocking up to ``timeout_seconds``.

        ``timeout_seconds=0`` blocks indefinitely (Redis convention).
        Returns None on timeout when the queue is empty.

        BRPOP pops from the right end while ``enqueue`` LPUSHes on
        the left — FIFO under normal load. Multiple workers can
        BRPOP the same queue concurrently and Redis hands each event
        to exactly one worker.
        """
        # redis-py's brpop returns Awaitable[list[bytes] | None]; the
        # asyncio Redis stub is always awaitable. Narrow ignore.
        result = await self._redis.brpop(
            [self._queue_key], timeout=int(timeout_seconds)
        )  # type: ignore[misc]
        if result is None:
            return None
        _, raw = result
        return _decode(raw)

    async def size(self) -> int:
        """Current queue length. Useful for ops dashboards / tests."""
        # redis-py's llen returns Awaitable[int]; same dual-stub quirk.
        n = await self._redis.llen(self._queue_key)  # type: ignore[misc]
        return int(n)


def _encode(event: PluginEvent) -> str:
    return json.dumps(
        {
            "event_id": str(event.event_id),
            "trigger": event.trigger,
            "firm_slug": event.firm_slug,
            "firm_id": str(event.firm_id),
            "event_data": event.event_data,
            "enqueued_at": event.enqueued_at.isoformat(),
        }
    )


def _decode(raw: bytes | str) -> PluginEvent:
    """Parse a queue payload back into ``PluginEvent``.

    Malformed payloads raise — they shouldn't be in the queue
    (every encode goes through ``_encode``), and silently dropping
    them would leak runs.
    """
    if isinstance(raw, bytes):
        raw = raw.decode()
    data = json.loads(raw)
    return PluginEvent(
        event_id=uuid.UUID(data["event_id"]),
        trigger=data["trigger"],
        firm_slug=data["firm_slug"],
        firm_id=uuid.UUID(data["firm_id"]),
        event_data=data["event_data"],
        enqueued_at=_dt.datetime.fromisoformat(data["enqueued_at"]),
    )
