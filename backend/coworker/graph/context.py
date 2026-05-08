"""FastAPI dependency `graph_context` — yields a per-request Graph bundle.

Every Microsoft Graph route resolves this dependency once. The
dependency:

1. Resolves the authenticated user via ``current_user``.
2. Re-enters ``firm_context(user.firm_id)`` for the request scope.
   ``current_user`` exits its own firm_context before returning, so
   we open a fresh one bound to the request scope here. (At the
   transaction layer the GUC was already set by ``current_user``'s
   first SELECT and persists for the open transaction; this is
   belt-and-braces so any new transaction started during the
   request inherits the firm scope automatically.)
3. Loads the firm row.
4. Decides whether to refresh the access token. The token is
   refreshed if any of:

   - ``ms_token_expires_at`` is NULL (incomplete onboarding row)
   - ``ms_token_expires_at`` is timezone-naive (defensive — the
     column is ``timestamptz`` so this should not happen, but
     comparing tz-naive to tz-aware would otherwise raise
     ``TypeError`` mid-request)
   - ``ms_token_expires_at`` is within ``TOKEN_REFRESH_BUFFER`` of
     now
   - ``ms_access_token_ciphertext`` is NULL (inconsistent row)

5. Yields a ``GraphContext`` carrying firm + user + plaintext
   access_token + the request session.

The buffer is five minutes. Microsoft's Graph access tokens are
good for 60 minutes; refreshing aggressively at the start of a
request that might run for several seconds avoids the race where
a token expires mid-call. Refreshing too aggressively is cheap
(one extra round trip and one extra audit row); refreshing too
late is a 401 partway through the route.
"""
import datetime as _dt
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.api.deps import current_user
from coworker.db.models.tenancy import Firm, User
from coworker.db.session import firm_context, get_session
from coworker.graph.auth import refresh_access_token
from coworker.security.encryption import decrypt_str

TOKEN_REFRESH_BUFFER = _dt.timedelta(minutes=5)


@dataclass(frozen=True, repr=False)
class GraphContext:
    """Per-request bundle for Microsoft Graph operations.

    Frozen so a route handler cannot accidentally mutate the firm
    or user pointers and end up issuing Graph calls with a stale
    access_token. The access_token snapshot is the one taken at
    dependency resolution time; a single request should not need
    a second refresh.
    """

    firm: Firm
    user: User
    access_token: str
    session: AsyncSession

    def __repr__(self) -> str:
        # Redacted repr — accidental logging of GraphContext (or any
        # object holding it) must not leak the access_token. The
        # loguru patcher also redacts known token patterns; this is
        # belt-and-braces.
        return (
            f"GraphContext(firm_id={self.firm.id}, user_id={self.user.id}, "
            "access_token=<redacted>)"
        )


async def graph_context(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AsyncIterator[GraphContext]:
    """FastAPI dependency yielding a `GraphContext`.

    The yield form is required because we wrap the request in
    ``firm_context``: cleanup must run when the route returns.
    """
    async with firm_context(user.firm_id):
        firm = (
            await session.execute(select(Firm).where(Firm.id == user.firm_id))
        ).scalar_one()

        access_token = await _resolve_access_token(session, user, firm)

        yield GraphContext(
            firm=firm,
            user=user,
            access_token=access_token,
            session=session,
        )


async def _resolve_access_token(
    session: AsyncSession, user: User, firm: Firm
) -> str:
    """Return a plaintext access token, refreshing if near expiry or missing.

    Refresh path commits the session so the new access token + the
    success audit row land together; this is the contract of
    ``refresh_access_token`` (helper flushes, caller commits). On
    failure the helper has already committed its own failure audit
    and raised — the raise propagates here, the route handler does
    not run, and FastAPI's default handling turns it into a 5xx.
    Phase 12 (logout + token revocation) will install a custom
    exception handler that maps ``ConnectorAuthError`` to 401 with
    a sign-in-again hint.
    """
    now = _dt.datetime.now(_dt.UTC)
    expires_at = user.ms_token_expires_at

    needs_refresh = (
        expires_at is None
        or expires_at.tzinfo is None
        or expires_at <= now + TOKEN_REFRESH_BUFFER
        or user.ms_access_token_ciphertext is None
    )

    if needs_refresh:
        access_token = await refresh_access_token(session, user, firm)
        await session.commit()
        return access_token

    # Type narrowing: the `needs_refresh` branch above covers the
    # ciphertext-is-None case, so by here it must be bytes. mypy
    # cannot narrow through the boolean OR; assert explicitly.
    assert user.ms_access_token_ciphertext is not None
    return decrypt_str(user.ms_access_token_ciphertext, firm_id=str(firm.id))
