"""Platform-wide sweep for ``last_missed_at`` subscriptions.

Pairs with ``coworker.workers.backfill_missed`` (the CLI) and
``coworker-backfill.timer`` (the systemd unit). Every tick:

1. Find every firm with at least one subscription that has
   ``last_missed_at IS NOT NULL`` (cross-firm read via the
   NO FORCE RLS bracket in ``coworker.db.firms``).
2. Per firm, enter ``firm_context`` and load every marked row.
3. For each row: resolve the per-user GraphContext (proactively
   refreshing the user's access token if near expiry), then call
   ``backfill_missed_for_subscription`` to list catch-up messages
   and re-enqueue them as synthetic ``email_received`` events.
4. Commit per firm so partial progress survives a mid-sweep
   crash.

Errors are isolated per row: a bad token for one user doesn't
abort sibling rows in the same firm.
"""
import datetime as _dt
import uuid
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coworker.connectors.exceptions import ConnectorError
from coworker.db.firms import list_active_firm_ids
from coworker.db.models import Firm, GraphSubscription, User
from coworker.db.session import firm_context
from coworker.graph.subscription_backfill import (
    backfill_missed_for_subscription,
)
from coworker.graph.user_context import resolve_user_graph_context
from coworker.workers.plugin_queue import PluginEventQueue


@dataclass
class MissedSweepResult:
    """Per-sweep summary.

    ``rows_visited`` counts every marked row we considered.
    ``actions`` counts outcomes by category: ``enqueued`` /
    ``cleared_empty`` / ``skipped_no_user`` / ``skipped_no_ctx``
    / ``crashed``. ``messages_enqueued`` is the running total of
    synthetic events posted to the queue. Counts let dashboards
    alert without parsing per-row logs.
    """

    firms_seen: int = 0
    rows_visited: int = 0
    messages_enqueued: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    firm_errors: list[str] = field(default_factory=list)

    def record(self, action: str, *, messages: int = 0) -> None:
        self.actions[action] = self.actions.get(action, 0) + 1
        self.messages_enqueued += messages


async def sweep_missed_backfill(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    queue: PluginEventQueue,
    now: _dt.datetime | None = None,
    firm_ids: list[uuid.UUID] | None = None,
) -> MissedSweepResult:
    """Run one pass of the platform-wide missed-notification backfill.

    Args:
        sessionmaker: shared async sessionmaker.
        queue: target queue for synthetic events. Same instance the
            webhook receiver uses — backfilled events look identical
            to normal deliveries (plus ``backfilled: true`` flag).
        now: injectable clock.
        firm_ids: optional override. When None (production), the
            sweep calls ``list_active_firm_ids``. Tests pass an
            explicit list so a shared DB doesn't leak in other
            tests' firms.

    Returns:
        ``MissedSweepResult`` summarising what ran.
    """
    now = now if now is not None else _dt.datetime.now(_dt.UTC)
    result = MissedSweepResult()

    if firm_ids is None:
        async with sessionmaker() as session:
            firm_ids = await list_active_firm_ids(session)

    result.firms_seen = len(firm_ids)
    logger.info("missed backfill sweep firms={}", len(firm_ids))

    for firm_id in firm_ids:
        await _sweep_firm(
            firm_id=firm_id,
            sessionmaker=sessionmaker,
            queue=queue,
            now=now,
            result=result,
        )

    logger.info(
        "missed backfill sweep done firms={} rows={} enqueued={} actions={}",
        result.firms_seen,
        result.rows_visited,
        result.messages_enqueued,
        result.actions,
    )
    return result


async def _sweep_firm(
    *,
    firm_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    queue: PluginEventQueue,
    now: _dt.datetime,
    result: MissedSweepResult,
) -> None:
    async with sessionmaker() as session, firm_context(firm_id):
        firm = (
            await session.execute(select(Firm).where(Firm.id == firm_id))
        ).scalar_one_or_none()
        if firm is None:
            return

        rows = (
            await session.execute(
                select(GraphSubscription)
                .where(GraphSubscription.firm_id == firm_id)
                .where(GraphSubscription.last_missed_at.isnot(None))
            )
        ).scalars().all()

        if not rows:
            return

        for row in rows:
            result.rows_visited += 1
            await _sweep_row(
                session=session,
                firm=firm,
                row=row,
                queue=queue,
                now=now,
                result=result,
            )

        await session.commit()


async def _sweep_row(
    *,
    session: AsyncSession,
    firm: Firm,
    row: GraphSubscription,
    queue: PluginEventQueue,
    now: _dt.datetime,
    result: MissedSweepResult,
) -> None:
    user = (
        await session.execute(
            select(User).where(User.id == row.user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        logger.warning(
            "missed backfill row user missing sub_id={} user_id={}",
            row.subscription_id, row.user_id,
        )
        result.record("skipped_no_user")
        return

    ctx = await resolve_user_graph_context(session, firm=firm, user=user)
    if ctx is None:
        logger.warning(
            "missed backfill row no graph ctx sub_id={} user_id={}",
            row.subscription_id, user.id,
        )
        result.record("skipped_no_ctx")
        return

    try:
        outcome = await backfill_missed_for_subscription(
            session=session, ctx=ctx, queue=queue,
            row=row, firm_slug=firm.slug, now=now,
        )
    except ConnectorError as exc:
        logger.warning(
            "missed backfill row connector error sub_id={} err={}",
            row.subscription_id, exc,
        )
        result.record("connector_error")
        return
    except Exception:
        logger.exception(
            "missed backfill row crashed sub_id={}", row.subscription_id,
        )
        result.record("crashed")
        return

    if outcome.skipped:
        result.record(f"cleared_{outcome.skipped}")
        return
    result.record("enqueued", messages=outcome.enqueued)
