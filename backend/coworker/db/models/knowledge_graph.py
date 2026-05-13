"""Knowledge graph SQLAlchemy models — entities and their directed edges.

Entities are the people/companies/trusts/SMSFs/partnerships in a
firm's universe. Edges carry the relationship_type plus provenance
(where we learned it from, when, last validated). The KG populator
(Phase 4D) writes both sides; the retriever and Reactflow UI
(Phase 10) read them.
"""
import datetime as _dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base


class Entity(Base):
    """A node in the firm's knowledge graph.

    ``entity_type`` constrained at the application layer (the schema
    uses VARCHAR(50) to stay forward-compatible if XPM admins
    configure unusual types). Canonical values:

    - individual
    - company
    - trust
    - smsf
    - partnership

    ``xpm_client_id`` correlates the row with an XPM Client record
    when known; null for entities discovered through email/vision
    that have not yet matched an XPM record.
    """

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(500))

    xpm_client_id: Mapped[str | None] = mapped_column(String(100))
    abn: Mapped[str | None] = mapped_column(String(11))

    kg_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EntityRelationship(Base):
    """A directed edge between two entities.

    ``relationship_type`` is canonicalised at the application layer
    (the schema is free-text). Examples:

    - director_of (individual → company)
    - trustee_of (individual / company → trust)
    - beneficiary_of (individual → trust)
    - appointor_of (individual → trust)
    - shareholder_of (individual / company → company)
    - spouse_of (individual → individual)
    - parent_of (individual → individual)
    - member_of (individual → smsf)

    ``provenance`` records WHERE we learned this edge. Conventional
    shape::

        {
          "source": "xpm" | "trust_deed_pdf" | "company_extract" | "email",
          "source_id": "<document_id or xpm_relationship_id>",
          "first_seen": "<ISO timestamp>",
          "last_validated": "<ISO timestamp>"
        }

    The DB constrains ``from_entity_id <> to_entity_id`` so an edge
    can never be a self-loop. A unique partial index enforces "at
    most one active edge of a given type between the same pair" so
    the KG populator can UPSERT.
    """

    __tablename__ = "entity_relationships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
