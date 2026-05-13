"""Work-tracking SQLAlchemy models — jobs and deadlines.

``jobs`` mirrors XPM Job records so plugins can reason about
in-progress work without a round-trip to XPM. ``deadlines`` is
derived: BAS quarter ends, ASIC annual returns, tax-lodgement dates.
The Phase 4D KG populator writes both from XPM sync + email
extraction; Phase 6's bas_reminder / debtor_followup plugins read.
"""
import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class Job(Base):
    """A unit of accounting work — tax return, BAS lodgement, audit, etc.

    Mirrors XPM's Job records. ``xpm_id`` is null for jobs created
    locally without an XPM correlation. ``state`` is free-text from
    XPM (admins configure their own); common values:
    "in_progress", "complete", "on_hold", "cancelled".
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    xpm_id: Mapped[str | None] = mapped_column(String(100))
    client_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="in_progress",
        server_default="in_progress",
    )

    started_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    due_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    metadata_: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Deadline(Base):
    """A derived date that demands action.

    ``deadline_type`` examples: "bas_quarterly", "bas_monthly",
    "tax_return_individual", "tax_return_company", "asic_annual",
    "tfn_lodgement". Recurring deadlines carry an
    ``recurrence_pattern`` string (cron-like or "quarterly") and the
    scheduler creates the next instance after the current one
    transitions out of "pending".
    """

    __tablename__ = "deadlines"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    client_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )

    deadline_type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    due_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    is_recurring: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    recurrence_pattern: Mapped[str | None] = mapped_column(String(100))

    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending", server_default="pending"
    )

    metadata_: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
