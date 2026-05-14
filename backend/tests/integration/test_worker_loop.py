"""Tests for ``coworker.workers.loop.run_worker``.

The DB layer is real; Redis is the integration-test instance.
Stops are driven by ``asyncio.Event``; the BRPOP loop's poll
timeout is set short so tests don't wait on natural shutdown.
"""
import asyncio
import uuid
from urllib.parse import urlparse, urlunparse

import pytest_asyncio
from redis.asyncio import from_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.config import get_settings
from coworker.db.models import Firm
from coworker.db.session import _attach_pool_listeners, firm_context
from coworker.orchestrator.engine import ModelCallResult
from coworker.orchestrator.tools import ToolRegistry
from coworker.plugins.base import OrchestratorPlugin, PluginRegistry, PluginRun
from coworker.workers.loop import run_worker
from coworker.workers.plugin_queue import PluginEventQueue

_TEST_REDIS_DB = "/9"


def _test_redis_url() -> str:
    base = str(get_settings().REDIS_URL)
    parsed = urlparse(base)
    return urlunparse(parsed._replace(path=_TEST_REDIS_DB))


def _fresh_test_redis():
    return from_url(
        _test_redis_url(), encoding="utf-8", decode_responses=True
    )


class _EmailPlugin(OrchestratorPlugin):
    name = "loop_test_email"
    display_name = "Loop Test Email Plugin"
    description = "Test plugin for worker loop"
    triggers = frozenset({"email_received"})
    enabled_tool_categories = frozenset({"reasoning"})

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        return f"handle {run.event_data.get('message_id', 'X')}"


class ScriptedCaller:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        return ModelCallResult(
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            input_tokens=1,
            output_tokens=1,
            model="claude-sonnet-4-6",
        )


class _StubAnthropic:
    pass


def _stub_anthropic_factory(firm):
    return _StubAnthropic()  # type: ignore[return-value]


@pytest_asyncio.fixture
async def loop_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sm = async_sessionmaker(
        bind=engine, class_=AsyncSession,
        expire_on_commit=False, autoflush=False,
    )

    redis = _fresh_test_redis()
    await redis.flushdb()
    queue = PluginEventQueue(redis)

    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Loop Firm", slug=f"l-{uuid.uuid4().hex[:8]}"))
        await session.commit()

    try:
        yield {
            "sm": sm,
            "redis": redis,
            "queue": queue,
            "firm_id": firm_id,
        }
    finally:
        await _cleanup_firm(sm, firm_id)
        await redis.flushdb()
        await redis.aclose()
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    tables = (
        "firms", "users", "audit_log", "token_usage",
        "client_interactions", "lessons", "documents",
        "entity_relationships", "entities", "jobs", "deadlines",
        "agent_trace_steps", "agent_traces", "plugin_installations",
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
                "agent_trace_steps", "agent_traces",
                "plugin_installations",
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


def _registry() -> PluginRegistry:
    r = PluginRegistry()
    r.register(_EmailPlugin)
    return r


async def _wait_until_empty(queue, *, timeout: float = 3.0) -> None:
    """Poll the queue until it drains or the deadline expires."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if (await queue.size()) == 0:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"queue did not drain within {timeout}s; "
        f"size={await queue.size()}"
    )


# ===========================================================================
# Tests
# ===========================================================================


async def test_loop_exits_when_stop_event_set(loop_env) -> None:
    """An idle worker exits within one BRPOP cycle of stop_event.set()."""
    stop = asyncio.Event()
    caller = ScriptedCaller()
    task = asyncio.create_task(
        run_worker(
            queue=loop_env["queue"],
            sessionmaker=loop_env["sm"],
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            stop_event=stop,
            idle_poll_seconds=1,
        )
    )
    # Give the worker a moment to enter BRPOP.
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)
    assert task.done()
    assert task.exception() is None


async def test_loop_processes_enqueued_event(loop_env) -> None:
    """An event lpush'd into the queue triggers process_event."""
    queue = loop_env["queue"]
    firm_id = loop_env["firm_id"]
    # _EmailPlugin not installed for this firm — so the event will
    # be processed but skipped. That's still a process_event call,
    # which is what we're verifying.
    stop = asyncio.Event()
    caller = ScriptedCaller()
    task = asyncio.create_task(
        run_worker(
            queue=queue,
            sessionmaker=loop_env["sm"],
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            stop_event=stop,
            idle_poll_seconds=1,
        )
    )
    await queue.enqueue(
        trigger="email_received",
        firm_slug="loop-test",
        firm_id=firm_id,
        event_data={"message_id": "m1", "resource": "users/u/messages/m1"},
    )
    await _wait_until_empty(queue, timeout=3.0)
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)
    # Plugin wasn't installed -> caller never invoked, but loop ran.
    assert task.exception() is None


async def test_loop_processes_multiple_events_in_sequence(loop_env) -> None:
    """A worker drains a multi-event queue without dying."""
    queue = loop_env["queue"]
    firm_id = loop_env["firm_id"]
    stop = asyncio.Event()
    caller = ScriptedCaller()
    task = asyncio.create_task(
        run_worker(
            queue=queue,
            sessionmaker=loop_env["sm"],
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            stop_event=stop,
            idle_poll_seconds=1,
        )
    )
    for i in range(3):
        await queue.enqueue(
            trigger="email_received",
            firm_slug="loop-test",
            firm_id=firm_id,
            event_data={"message_id": f"m{i}"},
        )
    await _wait_until_empty(queue, timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None


async def test_loop_survives_malformed_payload(loop_env) -> None:
    """A non-JSON entry in the queue logs and the loop continues."""
    queue = loop_env["queue"]
    redis = loop_env["redis"]
    firm_id = loop_env["firm_id"]
    # Inject a malformed payload directly via the redis client.
    await redis.lpush("queue:plugin_events", "not-json{{{")
    # Follow with a valid event — if the loop dies on the bad payload,
    # this one stays in the queue and the drain wait will time out.
    await queue.enqueue(
        trigger="email_received",
        firm_slug="loop-test",
        firm_id=firm_id,
        event_data={"message_id": "after-bad"},
    )

    stop = asyncio.Event()
    caller = ScriptedCaller()
    task = asyncio.create_task(
        run_worker(
            queue=queue,
            sessionmaker=loop_env["sm"],
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            stop_event=stop,
            idle_poll_seconds=1,
        )
    )
    await _wait_until_empty(queue, timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None


async def test_loop_idle_wait_then_event(loop_env) -> None:
    """Worker idle on empty queue; event arrives mid-loop and gets processed."""
    queue = loop_env["queue"]
    firm_id = loop_env["firm_id"]
    stop = asyncio.Event()
    caller = ScriptedCaller()
    task = asyncio.create_task(
        run_worker(
            queue=queue,
            sessionmaker=loop_env["sm"],
            plugin_registry=_registry(),
            tool_registry=ToolRegistry(),
            model_caller=caller,
            anthropic_factory=_stub_anthropic_factory,
            stop_event=stop,
            idle_poll_seconds=1,
        )
    )
    # Let the worker idle a moment.
    await asyncio.sleep(0.3)
    await queue.enqueue(
        trigger="email_received",
        firm_slug="loop-test",
        firm_id=firm_id,
        event_data={"message_id": "late"},
    )
    await _wait_until_empty(queue, timeout=5.0)
    stop.set()
    await asyncio.wait_for(task, timeout=3.0)
    assert task.exception() is None
