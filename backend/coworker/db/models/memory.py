"""Memory-layer SQLAlchemy models — client interactions, lessons, documents.

These are the consumed-by-everyone rows: the hybrid retriever ranks
them, the orchestrator's memory tools fetch them, plugins write to
them. Every row carries a 1024-dim embedding and a weighted
tsvector; both indexes are HNSW (cosine) and GIN respectively, set
up by the e5f6a7b8c9d0 migration.

The Python ``tsv`` column is intentionally read-only from
application code — a BEFORE INSERT OR UPDATE trigger maintains it
from the text fields. Writing to ``tsv`` directly from SQLAlchemy
would be overwritten anyway; the column is exposed as a deferred-
load attribute so callers that want to inspect it can.
"""
import datetime as _dt
import uuid
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from coworker.db.base import Base

_EMBEDDING_DIM = 1024


class ClientInteraction(Base):
    """One meaningful interaction with (or about) a client.

    The retriever pulls these by hybrid BM25+vector+rerank. Subject
    weighted A, summary weighted B, body weighted C in the tsvector;
    the trigger handles concatenation.
    """

    __tablename__ = "client_interactions"

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

    interaction_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="email"
    )
    subject: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_EMBEDDING_DIM), nullable=True
    )
    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR, nullable=True, deferred=True
    )

    metadata_: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    occurred_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Lesson(Base):
    """A reflection-derived rule the orchestrator should apply.

    Phase 11's nightly reflection clusters approved-but-edited
    drafts into lessons; the retriever surfaces them with a
    priority weight; the decay machinery deactivates lessons whose
    ``decay_at`` is past and ``last_validated_at`` is more than 30
    days ago.
    """

    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(String(50))
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_EMBEDDING_DIM), nullable=True
    )
    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR, nullable=True, deferred=True
    )

    source: Mapped[str] = mapped_column(
        String(50), nullable=False, default="reflection",
        server_default="reflection",
    )
    last_validated_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    decay_at: Mapped[_dt.datetime | None] = mapped_column(
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


class Document(Base):
    """A file or KB entry consumable by the retriever and vision pipeline.

    ``source`` is the provenance namespace
    ('sharepoint' / 'email_attachment' / 'kb' / 'manual'). ``doc_type``
    is the vision-extracted classification ('noa' / 'financial_statement'
    / 'trust_deed' / etc.) or null for KB entries. ``spaces_url``
    points to the binary in DigitalOcean Spaces when applicable;
    inline KB documents leave it null.
    """

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    firm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("firms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    doc_type: Mapped[str | None] = mapped_column(String(50))
    client_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)

    spaces_url: Mapped[str | None] = mapped_column(Text)
    # SHA-256 hex of the source bytes. Used by the indexer to skip
    # re-extraction when a file is unchanged.
    content_hash: Mapped[str | None] = mapped_column(String(64))

    extracted_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_EMBEDDING_DIM), nullable=True
    )
    tsv: Mapped[Any | None] = mapped_column(
        TSVECTOR, nullable=True, deferred=True
    )

    indexed_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
