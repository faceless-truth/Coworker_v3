"""Per-user GraphContext builder with proactive token refresh.

Two consumers:

- ``coworker.workers.processor._resolve_graph_ctx_for_email`` calls
  this when a webhook-sourced email_received event fires.
- ``coworker.graph.missed_sweep`` calls this when reconciling rows
  with ``last_missed_at`` set.

Both paths share the same token-lifecycle policy: if the stored
``ms_access_token`` is within 5 minutes of expiry (or has no
expiry set), refresh via ``refresh_access_token`` and commit the
new ciphertext + expiry before returning the plaintext token.
"""
import datetime as _dt

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorTransient,
)
from coworker.db.models import Firm, User
from coworker.graph.auth import refresh_access_token
from coworker.graph.context import GraphContext
from coworker.security.encryption import decrypt_str

# How close to expiry we proactively refresh. Microsoft tokens
# usually live 1h; refreshing 5 minutes early absorbs clock skew
# and the round-trip cost of the refresh call without burning many
# tokens on too-eager rotation.
_TOKEN_REFRESH_BUFFER = _dt.timedelta(minutes=5)


async def resolve_user_graph_context(
    session: AsyncSession,
    *,
    firm: Firm,
    user: User,
) -> GraphContext | None:
    """Return a ready-to-use GraphContext for ``user``.

    Handles the "do we need to refresh?" decision, the refresh
    itself, and the GraphContext construction. Returns None when
    the user is missing tokens or the refresh failed permanently.

    Caller invariants:
    - ``session`` is inside ``firm_context(firm.id)`` already.
    - ``user.firm_id == firm.id``.
    """
    if user.ms_access_token_ciphertext is None:
        logger.warning(
            "graph user_context no ms_access_token user_id={}",
            user.id,
        )
        return None

    access_token = await resolve_user_access_token(
        session, firm=firm, user=user,
    )
    if access_token is None:
        return None

    return GraphContext(
        firm=firm, user=user, access_token=access_token, session=session,
    )


async def resolve_user_access_token(
    session: AsyncSession,
    *,
    firm: Firm,
    user: User,
) -> str | None:
    """Return a current plaintext access_token for ``user``.

    Three outcomes:
    - Stored token is fresh enough -> decrypt and return.
    - Near expiry or missing expiry -> refresh, commit, return the
      new plaintext token.
    - ConnectorAuthError -> None (user must sign in again).
    - ConnectorTransient -> log + fall back to the stored token so
      a one-off Microsoft 5xx doesn't drop the work in flight.

    Pre-condition: ``user.ms_access_token_ciphertext is not None``.
    """
    firm_id_str = str(firm.id)
    now = _dt.datetime.now(_dt.UTC)
    expires_at = user.ms_token_expires_at
    needs_refresh = (
        expires_at is None or expires_at <= now + _TOKEN_REFRESH_BUFFER
    )

    if needs_refresh:
        try:
            token = await refresh_access_token(session, user, firm)
            await session.commit()
            return token
        except ConnectorAuthError:
            logger.warning(
                "graph user_context token refresh rejected user_id={} — "
                "sign in again",
                user.id,
            )
            return None
        except ConnectorTransient:
            logger.warning(
                "graph user_context token refresh transient user_id={}; "
                "falling back to stored token",
                user.id,
            )
            # Fall through to the decrypt path.

    assert user.ms_access_token_ciphertext is not None  # caller checked
    try:
        return decrypt_str(
            user.ms_access_token_ciphertext, firm_id=firm_id_str,
        )
    except Exception:
        logger.exception(
            "graph user_context decrypt ms_access_token failed user_id={}",
            user.id,
        )
        return None
