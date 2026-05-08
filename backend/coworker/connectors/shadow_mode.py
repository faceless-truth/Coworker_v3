"""Shadow-mode guard — the gate every connector write method passes through.

A firm in shadow mode never produces external side effects: drafts
are not created, calendar events are not booked, FuseSign envelopes
are not sent, XPM client notes are not written. Reads continue
normally; only writes are blocked.

This module provides the single function that every connector write
method calls before touching an external system::

    await guard_writable(session, firm, action="email.create_draft")
    # ...issue the actual Graph call...

If ``firm.shadow_mode`` is True, ``guard_writable`` audits a
``shadow_blocked.{action}`` entry and raises ``ShadowModeBlocked``.
The connector method never reaches the external API; the audit row
captures the intent so the principal reviewing shadow-mode activity
can see what *would* have happened.

Centralising the gate in this module — rather than reimplementing
the check in each connector — removes the only realistic way the
property could be subverted: forgetting to check. The orchestrator
and plugin layers never call connector writes directly without
going through the connector's published method, and every published
write method calls ``guard_writable`` first.

The shadow-mode boolean lives on the Firm row and flips through an
explicit ceremony (Phase 13 onboarding wizard's graduation step).
There is no env-var bypass: SHADOW_MODE_OVERRIDE_FIRMS exists in
Settings but is intentionally not consulted here. Forcing every
firm through the same graduation ceremony keeps the audit trail
clean — every transition between shadow and live is captured as a
deliberate action by a named principal.
"""
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.connectors.exceptions import ConnectorError
from coworker.db.models.tenancy import Firm
from coworker.security.audit import append_audit


class ShadowModeBlocked(ConnectorError):
    """Write attempted while firm is in shadow mode.

    Carries the action string so callers can surface it to humans
    ("Your firm is in shadow mode; the email.create_draft action was
    not performed").
    """

    def __init__(self, action: str) -> None:
        super().__init__(f"shadow mode blocks write action: {action}")
        self.action = action


async def guard_writable(
    session: AsyncSession,
    firm: Firm,
    *,
    action: str,
    actor_id: str = "system",
    actor_type: str = "system",
) -> None:
    """Block ``action`` if ``firm.shadow_mode`` is True; otherwise no-op.

    On block: writes a ``shadow_blocked.{action}`` audit row, commits,
    and raises ``ShadowModeBlocked``. The commit is inline so the
    audit lands even if the caller's exception handling discards the
    session.

    Caller invariants:
      - ``firm_context(firm.id)`` already entered (so the audit row's
        RLS-scoped INSERT can land).
      - ``firm`` is the live row; do not pass a stale snapshot from
        a prior request.

    Args:
        session: AsyncSession the caller is using.
        firm: the Firm row whose shadow_mode is checked.
        action: short identifier of the action being attempted, e.g.
            ``email.create_draft``, ``fusesign.create_envelope``. The
            audit action becomes ``shadow_blocked.{action}``.
        actor_id: who attempted the action. Defaults to "system" for
            scheduled / agent-initiated calls; user-initiated calls
            should pass ``str(user.id)``.
        actor_type: ``user`` or ``system`` (matches the audit log's
            actor_type column).
    """
    if not firm.shadow_mode:
        return

    await append_audit(
        session,
        firm_id=str(firm.id),
        actor_type=actor_type,
        actor_id=actor_id,
        action=f"shadow_blocked.{action}",
        payload={"action": action, "actor_id": actor_id},
    )
    await session.commit()
    raise ShadowModeBlocked(action=action)
