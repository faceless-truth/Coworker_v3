"""Phase 4A: memory layer + knowledge graph schema

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-13 02:00:00.000000

Creates seven new tables for Phase 4's memory architecture:

- ``client_interactions`` — every meaningful email or conversation
  with a client. Subject + summary + body, embedding(1024), weighted
  tsvector (subject A, summary B, body C).
- ``lessons`` — Phase 9/11 reflection output. Text + embedding +
  tsvector, priority, decay tracking.
- ``documents`` — SharePoint files, email attachments, KB documents.
  Title + summary + body, embedding, tsvector, extracted_data JSONB,
  Spaces pointer.
- ``entities`` — the knowledge graph's nodes: individuals, companies,
  trusts, SMSFs, partnerships.
- ``entity_relationships`` — directed edges between entities
  (director_of / trustee_of / beneficiary_of / etc.) with provenance.
- ``jobs`` — XPM-mirrored work items (tax returns, BAS, audits).
- ``deadlines`` — derived dates that demand action (BAS quarter ends,
  ASIC returns, etc.).

Indexes:

- HNSW (cosine) on every ``embedding`` column.
- GIN on every ``tsv`` column.
- B-tree on ``(firm_id, created_at DESC)`` for activity feeds.
- Per-table secondary indexes where the query plans need them.

Triggers maintain each ``tsv`` column from the text fields with the
appropriate setweight weights — no application code touches tsv.

RLS+FORCE with the four standard policies on the ``app.firm_id``
GUC, matching Phase 2.1.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TENANT_TABLES_4A = (
    "client_interactions",
    "lessons",
    "documents",
    "entities",
    "entity_relationships",
    "jobs",
    "deadlines",
)


def _rls_match() -> str:
    return "firm_id = NULLIF(current_setting('app.firm_id', true), '')::uuid"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    match = _rls_match()
    op.execute(
        f"CREATE POLICY {table}_firm_isolation_select ON {table} "
        f"FOR SELECT USING ({match})"
    )
    op.execute(
        f"CREATE POLICY {table}_firm_isolation_insert ON {table} "
        f"FOR INSERT WITH CHECK ({match})"
    )
    op.execute(
        f"CREATE POLICY {table}_firm_isolation_update ON {table} "
        f"FOR UPDATE USING ({match}) WITH CHECK ({match})"
    )
    op.execute(
        f"CREATE POLICY {table}_firm_isolation_delete ON {table} "
        f"FOR DELETE USING ({match})"
    )


def upgrade() -> None:
    # ------------------------------------------------------------------
    # entities — created first so other tables can FK to it.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE entities (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            entity_type VARCHAR(50) NOT NULL,
            name VARCHAR(500) NOT NULL,
            display_name VARCHAR(500),
            xpm_client_id VARCHAR(100),
            abn VARCHAR(11),
            kg_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_entities_firm_created ON entities "
        "(firm_id, created_at DESC)"
    )
    # XPM correlation lookup — sparse, mostly unique within a firm.
    op.execute(
        "CREATE INDEX ix_entities_firm_xpm ON entities "
        "(firm_id, xpm_client_id) WHERE xpm_client_id IS NOT NULL"
    )
    # Case-insensitive name lookup uses pg_trgm — extension created in
    # the a1b2c3d4e5f6 migration. UUID columns lack a default GIN
    # operator class, so firm_id is left out of this index;
    # postgres prefilters via RLS and the trigram scan stays scoped.
    op.execute(
        "CREATE INDEX ix_entities_name_trgm ON entities "
        "USING gin (name gin_trgm_ops)"
    )

    # ------------------------------------------------------------------
    # client_interactions
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE client_interactions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            client_entity_id UUID REFERENCES entities(id) ON DELETE SET NULL,
            interaction_type VARCHAR(50) NOT NULL DEFAULT 'email',
            subject TEXT,
            summary TEXT,
            body TEXT,
            embedding vector(1024),
            tsv tsvector,
            metadata_ JSONB NOT NULL DEFAULT '{}'::jsonb,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_client_interactions_firm_occurred ON client_interactions "
        "(firm_id, occurred_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_client_interactions_firm_entity ON client_interactions "
        "(firm_id, client_entity_id) WHERE client_entity_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_client_interactions_embedding ON client_interactions "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_client_interactions_tsv ON client_interactions "
        "USING gin (tsv)"
    )
    op.execute(
        """
        CREATE FUNCTION client_interactions_tsv_update() RETURNS trigger AS $$
        BEGIN
          NEW.tsv :=
            setweight(to_tsvector('english', coalesce(NEW.subject, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(NEW.body,    '')), 'C');
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER client_interactions_tsv_trigger "
        "BEFORE INSERT OR UPDATE ON client_interactions "
        "FOR EACH ROW EXECUTE FUNCTION client_interactions_tsv_update()"
    )

    # ------------------------------------------------------------------
    # lessons
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE lessons (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            category VARCHAR(50),
            priority INTEGER NOT NULL DEFAULT 1,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            embedding vector(1024),
            tsv tsvector,
            source VARCHAR(50) NOT NULL DEFAULT 'reflection',
            last_validated_at TIMESTAMPTZ,
            decay_at TIMESTAMPTZ,
            metadata_ JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_lessons_firm_created ON lessons "
        "(firm_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_lessons_firm_active ON lessons "
        "(firm_id, is_active, priority DESC) WHERE is_active = TRUE"
    )
    op.execute(
        "CREATE INDEX ix_lessons_embedding ON lessons "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_lessons_tsv ON lessons USING gin (tsv)"
    )
    op.execute(
        """
        CREATE FUNCTION lessons_tsv_update() RETURNS trigger AS $$
        BEGIN
          NEW.tsv := setweight(to_tsvector('english', coalesce(NEW.text, '')), 'A');
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER lessons_tsv_trigger "
        "BEFORE INSERT OR UPDATE ON lessons "
        "FOR EACH ROW EXECUTE FUNCTION lessons_tsv_update()"
    )

    # ------------------------------------------------------------------
    # documents
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            source VARCHAR(50) NOT NULL,
            doc_type VARCHAR(50),
            client_entity_id UUID REFERENCES entities(id) ON DELETE SET NULL,
            title VARCHAR(500),
            summary TEXT,
            body TEXT,
            spaces_url TEXT,
            content_hash CHAR(64),
            extracted_data JSONB NOT NULL DEFAULT '{}'::jsonb,
            embedding vector(1024),
            tsv tsvector,
            indexed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_documents_firm_created ON documents "
        "(firm_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_documents_firm_type ON documents "
        "(firm_id, doc_type) WHERE doc_type IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_documents_firm_hash ON documents "
        "(firm_id, content_hash) WHERE content_hash IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_documents_embedding ON documents "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_documents_tsv ON documents USING gin (tsv)"
    )
    op.execute(
        """
        CREATE FUNCTION documents_tsv_update() RETURNS trigger AS $$
        BEGIN
          NEW.tsv :=
            setweight(to_tsvector('english', coalesce(NEW.title,   '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.summary, '')), 'B') ||
            setweight(to_tsvector('english', coalesce(NEW.body,    '')), 'C');
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER documents_tsv_trigger "
        "BEFORE INSERT OR UPDATE ON documents "
        "FOR EACH ROW EXECUTE FUNCTION documents_tsv_update()"
    )

    # ------------------------------------------------------------------
    # entity_relationships — directed edges
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE entity_relationships (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            from_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            to_entity_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            relationship_type VARCHAR(100) NOT NULL,
            provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
            confidence REAL NOT NULL DEFAULT 1.0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT entity_relationships_no_self_loop
                CHECK (from_entity_id <> to_entity_id)
        )
        """
    )
    # Edges by source — the typical KG-walk direction.
    op.execute(
        "CREATE INDEX ix_entity_relationships_firm_from "
        "ON entity_relationships (firm_id, from_entity_id)"
    )
    # And by destination — reverse-walk for "who's related to this client?".
    op.execute(
        "CREATE INDEX ix_entity_relationships_firm_to "
        "ON entity_relationships (firm_id, to_entity_id)"
    )
    # Dedup index: at most one active edge of a given type between
    # the same pair. Allows the populator to UPSERT cleanly.
    op.execute(
        "CREATE UNIQUE INDEX ix_entity_relationships_unique_active "
        "ON entity_relationships "
        "(firm_id, from_entity_id, to_entity_id, relationship_type) "
        "WHERE is_active = TRUE"
    )

    # ------------------------------------------------------------------
    # jobs — XPM mirror
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            xpm_id VARCHAR(100),
            client_entity_id UUID REFERENCES entities(id) ON DELETE SET NULL,
            name VARCHAR(500) NOT NULL,
            state VARCHAR(50) NOT NULL DEFAULT 'in_progress',
            started_at TIMESTAMPTZ,
            due_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            metadata_ JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_jobs_firm_due ON jobs "
        "(firm_id, due_at) WHERE state <> 'completed' AND due_at IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_jobs_firm_xpm "
        "ON jobs (firm_id, xpm_id) WHERE xpm_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_jobs_firm_entity ON jobs "
        "(firm_id, client_entity_id) WHERE client_entity_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # deadlines — derived dates that demand action
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE deadlines (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            firm_id UUID NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
            client_entity_id UUID REFERENCES entities(id) ON DELETE SET NULL,
            deadline_type VARCHAR(50) NOT NULL,
            title VARCHAR(500) NOT NULL,
            due_at TIMESTAMPTZ NOT NULL,
            is_recurring BOOLEAN NOT NULL DEFAULT FALSE,
            recurrence_pattern VARCHAR(100),
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            metadata_ JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_deadlines_firm_due ON deadlines "
        "(firm_id, due_at) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_deadlines_firm_entity ON deadlines "
        "(firm_id, client_entity_id) WHERE client_entity_id IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # RLS on every new tenant table
    # ------------------------------------------------------------------
    for table in _TENANT_TABLES_4A:
        _enable_rls(table)


def downgrade() -> None:
    # Drop policies first (FORCE RLS, the policies have hard names).
    for table in _TENANT_TABLES_4A:
        for op_name in ("select", "insert", "update", "delete"):
            op.execute(
                f"DROP POLICY IF EXISTS "
                f"{table}_firm_isolation_{op_name} ON {table}"
            )

    # Drop triggers + functions (each table that has a tsv trigger).
    for table in ("client_interactions", "lessons", "documents"):
        op.execute(
            f"DROP TRIGGER IF EXISTS {table}_tsv_trigger ON {table}"
        )
        op.execute(f"DROP FUNCTION IF EXISTS {table}_tsv_update()")

    # Drop tables in reverse dependency order. entity_relationships
    # FKs entities; documents/jobs/deadlines/client_interactions FK
    # entities; everything FKs firms (untouched by this downgrade).
    op.execute("DROP TABLE IF EXISTS deadlines CASCADE")
    op.execute("DROP TABLE IF EXISTS jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS entity_relationships CASCADE")
    op.execute("DROP TABLE IF EXISTS documents CASCADE")
    op.execute("DROP TABLE IF EXISTS lessons CASCADE")
    op.execute("DROP TABLE IF EXISTS client_interactions CASCADE")
    op.execute("DROP TABLE IF EXISTS entities CASCADE")
