"""Xero Practice Manager (XPM) connector.

Single per-firm class. The only code path in this codebase that talks
to ``identity.xero.com`` (token endpoint) or ``api.xero.com`` (XPM
REST API).

OAuth shape (Xero's standard for confidential clients):

- Token endpoint: ``https://identity.xero.com/connect/token``.
- ``refresh_token`` grant authenticated with HTTP Basic
  (client_id : client_secret).
- Refresh tokens valid 60 days, **single-use** — every refresh returns
  a new refresh_token that replaces the previous one. The connector
  rotates the persisted ciphertext on every successful refresh; if
  the rotation isn't committed to the DB, the next refresh will fail
  with invalid_grant.
- Access tokens valid ~30 minutes. We refresh proactively at the
  5-minute mark so a slow downstream call doesn't time out mid-flight.

Audit shape:

- Success: ``xpm.token_refreshed`` with the new expiry.
- Failure: ``xpm.token_refresh_failed`` with the reason.

For Phase 3 the actor on these rows defaults to ``"system"`` because
the orchestrator and background workflows are the typical callers.
A user-initiated path (e.g. a future UI button "refresh XPM
connection") can construct an ``XPMClient`` with ``actor_type="user"``
and ``actor_id=str(user.id)`` and the audit row will reflect that.

This commit (Phase 3E-2) lands the OAuth scaffolding only —
``_ensure_access_token`` / ``_refresh_access_token`` plus the class
shape and credential checks. The read and write methods (
``list_clients``, ``get_client``, ``create_client_note``, etc.) land
in subsequent Phase 3E sub-commits.
"""
import datetime as _dt
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorTransient,
)
from coworker.db.models.tenancy import Firm
from coworker.security.audit import append_audit
from coworker.security.encryption import decrypt_str, encrypt_str

_TOKEN_ENDPOINT = "https://identity.xero.com/connect/token"
# XPM's base URL. The "3.1" segment matches the current Practice
# Manager API version; if Xero versions the path we'll update one
# constant. The trailing slash is omitted so callers compose
# ``f"{_API_BASE}/{resource}"`` cleanly.
_API_BASE = "https://api.xero.com/practicemanager/3.1"

_TOKEN_REFRESH_BUFFER = _dt.timedelta(minutes=5)
# Xero documents 30-min access tokens, but the actual expires_in
# field on the response is authoritative. This default only applies
# if the response omits expires_in (it doesn't, in practice — kept
# for defensive parsing).
_DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 1800

SYSTEM_ACTOR = "system"


class XPMClient:
    """Per-firm XPM REST client.

    Construct once per (firm, session) pair. The class holds a
    reference to the Firm row so token refreshes mutate it in place;
    the caller's session is the one those mutations land on. Callers
    are responsible for committing the session — the refresh helper
    flushes but does not commit, so audit rows + token rotation land
    in the same transaction as whatever XPM call triggered the
    refresh.

    Args:
        firm: target firm. Must have ``xpm_client_id``,
            ``xpm_client_secret_ciphertext`` and
            ``xpm_refresh_token_ciphertext`` populated (set during
            the Phase 13 onboarding wizard).
        session: AsyncSession; used to persist refreshed tokens and
            write audit rows.
        actor_id: who's making this call. Defaults to ``"system"``
            because the orchestrator and background jobs are the
            typical callers. User-initiated paths pass
            ``str(user.id)`` and ``actor_type="user"``.
        actor_type: ``"system"`` (default) or ``"user"``.
    """

    def __init__(
        self,
        firm: Firm,
        *,
        session: AsyncSession,
        actor_id: str = SYSTEM_ACTOR,
        actor_type: Literal["user", "system"] = "system",
    ) -> None:
        self._firm = firm
        self._session = session
        self._actor_id = actor_id
        self._actor_type = actor_type

    @property
    def firm(self) -> Firm:
        return self._firm

    async def _ensure_access_token(self) -> str:
        """Return a non-expired XPM access token, refreshing if needed.

        Refresh trigger: ``xpm_token_expires_at`` is None, in the
        past, or within ``_TOKEN_REFRESH_BUFFER`` of now. Otherwise
        the cached ciphertext is decrypted and returned.
        """
        firm = self._firm
        now = _dt.datetime.now(_dt.UTC)
        expires_at = firm.xpm_token_expires_at

        if (
            expires_at is None
            or expires_at.tzinfo is None
            or expires_at <= now + _TOKEN_REFRESH_BUFFER
            or firm.xpm_access_token_ciphertext is None
        ):
            return await self._refresh_access_token()

        return decrypt_str(
            firm.xpm_access_token_ciphertext, firm_id=str(firm.id)
        )

    async def _refresh_access_token(self) -> str:
        """Run the refresh_token grant; rotate persisted tokens.

        Persists the new access and refresh tokens (Xero rotates the
        refresh token on every grant) plus the new expiry. Audits
        ``xpm.token_refreshed`` on success, ``xpm.token_refresh_failed``
        on every failure mode. Returns the new plaintext access token.

        Raises:
            ConnectorAuthError: missing credentials on the firm row,
                or Xero rejected the grant (4xx — typically
                ``invalid_grant`` when the refresh token is past 60
                days or has been revoked).
            ConnectorTransient: 5xx from Xero or network error.
        """
        firm = self._firm
        firm_id_str = str(firm.id)

        # Credential precondition checks. Surface as audited
        # ConnectorAuthError so the caller sees the same exception
        # family regardless of whether Xero rejected us or we never
        # had credentials to send.
        if firm.xpm_refresh_token_ciphertext is None:
            await self._audit_refresh_failure("missing_refresh_token")
            raise ConnectorAuthError(
                f"firm {firm.id} has no xpm_refresh_token; XPM not connected"
            )
        if (
            firm.xpm_client_id is None
            or firm.xpm_client_secret_ciphertext is None
        ):
            await self._audit_refresh_failure("missing_client_credentials")
            raise ConnectorAuthError(
                f"firm {firm.id} has no XPM client credentials"
            )

        # Decrypt outside the try/except so a corrupt-ciphertext error
        # is its own thing, not lumped with HTTP errors.
        try:
            refresh_token = decrypt_str(
                firm.xpm_refresh_token_ciphertext, firm_id=firm_id_str
            )
            client_secret = decrypt_str(
                firm.xpm_client_secret_ciphertext, firm_id=firm_id_str
            )
        except Exception:
            await self._audit_refresh_failure("corrupt_ciphertext")
            raise ConnectorAuthError(
                f"firm {firm.id} XPM ciphertext could not be decrypted"
            ) from None

        try:
            async with httpx.AsyncClient(timeout=30) as http:
                response = await http.post(
                    _TOKEN_ENDPOINT,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    auth=(firm.xpm_client_id, client_secret),
                )
        except httpx.RequestError as exc:
            await self._audit_refresh_failure("network_error")
            raise ConnectorTransient(
                "network error talking to Xero identity"
            ) from exc

        status = response.status_code
        if 200 <= status < 300:
            body = response.json()
            new_access: str = body["access_token"]
            # Xero may or may not return a new refresh_token on every
            # grant. In their current implementation it always
            # rotates, but we defensively fall back to the existing
            # refresh token if the field is absent — that keeps the
            # firm row consistent if Xero ever changes behaviour.
            new_refresh: str = body.get("refresh_token", refresh_token)
            expires_in = int(
                body.get("expires_in", _DEFAULT_ACCESS_TOKEN_TTL_SECONDS)
            )

            firm.xpm_access_token_ciphertext = encrypt_str(
                new_access, firm_id=firm_id_str
            )
            firm.xpm_refresh_token_ciphertext = encrypt_str(
                new_refresh, firm_id=firm_id_str
            )
            firm.xpm_token_expires_at = (
                _dt.datetime.now(_dt.UTC)
                + _dt.timedelta(seconds=expires_in)
            )
            await self._session.flush()
            await append_audit(
                self._session,
                firm_id=firm_id_str,
                actor_type=self._actor_type,
                actor_id=self._actor_id,
                action="xpm.token_refreshed",
                payload={
                    "user_id": self._actor_id,
                    "expires_in": expires_in,
                },
            )
            await self._session.commit()
            return new_access

        if 500 <= status < 600:
            await self._audit_refresh_failure("xero_5xx")
            raise ConnectorTransient(
                f"Xero identity returned {status} on refresh"
            )
        # 4xx — invalid_grant (refresh token expired / revoked /
        # already used), invalid_client (credentials wrong), etc.
        await self._audit_refresh_failure(f"xero_{status}")
        raise ConnectorAuthError(
            f"Xero identity rejected refresh: HTTP {status}"
        )

    async def _audit_refresh_failure(self, reason: str) -> None:
        """Append ``xpm.token_refresh_failed`` and commit.

        Mirrors ``graph.auth._audit_failure_and_commit``: the audit row
        is committed inline so it survives any subsequent rollback in
        the caller's transaction (the calling XPM method may abort
        before committing its own work).
        """
        firm_id_str = str(self._firm.id)
        payload: dict[str, Any] = {
            "user_id": self._actor_id,
            "reason": reason,
        }
        await append_audit(
            self._session,
            firm_id=firm_id_str,
            actor_type=self._actor_type,
            actor_id=self._actor_id,
            action="xpm.token_refresh_failed",
            payload=payload,
        )
        await self._session.commit()
