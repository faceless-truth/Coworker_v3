import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from coworker.config import get_settings


# Per-async-task firm context. Set by request handlers (FastAPI route via
# Depends, or test fixtures) using the firm_context() async context
# manager. The Session "after_begin" listener below reads this value at
# transaction start and applies it as a transaction-scoped GUC so RLS
# policies can filter on it. Default of None means "no firm context" —
# the listener does nothing, app.firm_id stays unset, and RLS policies
# treat the predicate as NULL → zero rows visible. That is the
# secure-by-default property: forgetting to set the firm context
# returns no rows rather than every row.
current_firm_id: ContextVar[uuid.UUID | None] = ContextVar(
    "current_firm_id", default=None
)


@asynccontextmanager
async def firm_context(firm_id: uuid.UUID) -> AsyncIterator[None]:
    """Bind firm_id to the current async task for the duration of the block.

    Uses ContextVar.set() / ContextVar.reset(token) rather than a manual
    save-and-restore. The token returned by set() restores to whatever
    value was active immediately before THIS set call, so nested
    firm_context() blocks compose correctly even if the outer block had
    a different firm or no firm at all.
    """
    token = current_firm_id.set(firm_id)
    try:
        yield
    finally:
        current_firm_id.reset(token)


@event.listens_for(Session, "after_begin")
def _apply_firm_id_on_transaction_start(
    session: Session, transaction: Any, connection: Any
) -> None:
    """Apply the firm_context GUC at the start of every transaction.

    Registered on the synchronous Session class because AsyncSession
    delegates to a sync Session under the hood and the event dispatcher
    fires there. The handler runs inside the Python execution context
    of the calling async task, so ContextVar.get() returns the value
    set by firm_context().

    If no firm context is active we do nothing — leaving app.firm_id
    unset is what gives RLS its secure-by-default behaviour. We must
    not "clear" the GUC here because that would issue a query and
    waste a round trip on every transaction, including transactions
    that legitimately don't need a firm context (admin tooling,
    superuser maintenance, etc.).

    Uses set_config('app.firm_id', :firm_id, true) rather than
    SET LOCAL app.firm_id = '<uuid>' because set_config() is a regular
    SQL function with parameter binding via SQLAlchemy text(). SET LOCAL
    is a top-level SQL statement whose value must be a literal, which
    would force us to interpolate the UUID string into the SQL — safe
    in practice for UUIDs but worse hygiene. The third argument `true`
    makes the assignment transaction-scoped, equivalent to SET LOCAL.
    """
    firm_id = current_firm_id.get()
    if firm_id is None:
        return
    connection.execute(
        text("SELECT set_config('app.firm_id', :firm_id, true)"),
        {"firm_id": str(firm_id)},
    )


def _attach_pool_listeners(engine: AsyncEngine) -> None:
    """Belt-and-braces: clear app.firm_id when a connection returns to the pool.

    Our normal code path uses set_config(..., is_local=true) which is
    transaction-scoped, so the GUC is automatically discarded at COMMIT
    or ROLLBACK. This handler covers the case where some other code
    path sets the GUC at session level (without LOCAL) — connection
    reuse must not leak firm context across requests.

    Registered on engine.sync_engine because the SQLAlchemy pool events
    fire on the sync engine even for AsyncEngine. The handler executes
    on the raw DBAPI connection through the asyncpg sync adapter.

    Exposed as a module-level helper (not inlined into get_engine) so
    tests that build their own engine — notably the pool-reuse test
    that needs pool_size=1 — can attach the same listeners.
    """
    @event.listens_for(engine.sync_engine, "checkin")
    def _reset_firm_id_on_checkin(
        dbapi_connection: Any, connection_record: Any
    ) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("RESET app.firm_id")
        finally:
            cursor.close()


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Construct the async engine lazily on first use.

    Deferring construction means `import coworker.db.session` does not require
    DATABASE_URL/REDIS_URL/etc. to be set — important for unit tests that never
    touch the DB and for tools (CLI introspection, mypy) that import the module
    without an env.
    """
    settings = get_settings()
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_POOL_MAX_OVERFLOW,
        pool_pre_ping=True,
        echo=False,
    )
    _attach_pool_listeners(engine)
    return engine


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def __getattr__(name: str) -> Any:
    """PEP 562 lazy module attributes.

    Preserves `from coworker.db.session import engine` and
    `from coworker.db.session import SessionLocal` as before, but the underlying
    objects are only built on first access.
    """
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_sessionmaker()
    raise AttributeError(f"module 'coworker.db.session' has no attribute {name!r}")


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an AsyncSession.

    Bare async-generator pattern (no @asynccontextmanager) so it works
    correctly with `Depends(get_session)`. Callers must commit explicitly;
    the surrounding `async with` only handles close/rollback on exception.

    The firm context (RLS scope) is set by the CALLER, not here — a
    route handler calls `async with firm_context(firm_id):` around its
    DB work, and the after_begin event listener picks it up. Keeping
    that responsibility at the call site means get_session is reusable
    from contexts where firm scope is supplied differently (CLI, system
    jobs, tests with bespoke fixtures).
    """
    async with get_sessionmaker()() as session:
        yield session
