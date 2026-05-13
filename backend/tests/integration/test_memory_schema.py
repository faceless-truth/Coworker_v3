"""Smoke tests for the Phase 4A memory + KG schema.

Verifies the new tables are usable from SQLAlchemy under RLS:
INSERTs land, FKs hold, the tsv trigger maintains the full-text
column, embeddings round-trip, and the unique-active partial index
on entity_relationships prevents duplicate edges.

This is intentionally light-touch — the hybrid retriever and KG
populator tests (Phases 4B/4C/4D) exercise the schema more
thoroughly. For 4A we just need to know the schema is wired up
correctly.
"""
import datetime as _dt
import uuid

import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from coworker.db.models import (
    ClientInteraction,
    Deadline,
    Document,
    Entity,
    EntityRelationship,
    Firm,
    Job,
    Lesson,
)
from coworker.db.session import _attach_pool_listeners, firm_context


@pytest_asyncio.fixture
async def memory_env(test_database_url):
    engine = create_async_engine(test_database_url, poolclass=NullPool)
    _attach_pool_listeners(engine)
    sessionmaker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    created_firm_ids: list[uuid.UUID] = []
    try:
        yield {"sessionmaker": sessionmaker, "created_firm_ids": created_firm_ids}
    finally:
        for firm_id in created_firm_ids:
            await _cleanup_firm(sessionmaker, firm_id)
        await engine.dispose()


async def _cleanup_firm(sm, firm_id):
    """Strip every cascade-able row for the firm then drop the firm.

    NO FORCE / DELETE / FORCE bracket — the deletes inside the same
    transaction span tables in dependency order (children first) so
    the FK cascade works without violating constraints. A failure
    in any DELETE aborts the bracket transaction, which would also
    abort the FORCE re-enable in the finally block; we therefore
    issue the bracket toggle and the deletes in separate
    transactions so cleanup is robust to any one statement failing.
    """
    tables = (
        "firms",
        "users",
        "audit_log",
        "token_usage",
        "client_interactions",
        "lessons",
        "documents",
        "entity_relationships",
        "entities",
        "jobs",
        "deadlines",
    )
    # Per-table NO FORCE in one short transaction.
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
            )
        await session.commit()

    # Deletes in a separate transaction. firm_id-keyed for the
    # tenant tables; firms uses id.
    async with sm() as session:
        try:
            for t in (
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

    # Re-enable FORCE in its own transaction so it lands even if a
    # delete above failed.
    async with sm() as session:
        for t in tables:
            await session.execute(
                text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
            )
        await session.commit()


async def _seed_firm(sm) -> uuid.UUID:
    firm_id = uuid.uuid4()
    async with sm() as session, firm_context(firm_id):
        session.add(Firm(id=firm_id, name="Memory Smoke", slug=f"mem-{uuid.uuid4().hex[:8]}"))
        await session.commit()
    return firm_id


# =========================================================================
# Smoke tests
# =========================================================================


async def test_client_interaction_inserts_and_tsv_trigger_fires(
    memory_env,
) -> None:
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        ci = ClientInteraction(
            firm_id=firm_id,
            interaction_type="email",
            subject="Quarterly BAS",
            summary="Client provided figures for the June quarter",
            body="The total GST collected was $11,000 and the GST paid was $4,500.",
        )
        session.add(ci)
        await session.commit()
        ci_id = ci.id

    # Re-fetch and verify the trigger populated tsv from the text fields.
    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                text(
                    "SELECT tsv::text FROM client_interactions WHERE id = :id"
                ),
                {"id": str(ci_id)},
            )
        ).scalar_one()
        # The tsv text representation includes lexemes with weight markers.
        # Subject 'Quarterly BAS' weighted A; summary words weighted B;
        # body words weighted C. We don't try to assert exact tsvector
        # content (Postgres normalises lexemes), just that the column is
        # non-empty and contains expected words.
        assert row
        assert "quarter" in row.lower()
        assert "bas" in row.lower()


async def test_lesson_inserts_with_embedding_round_trip(memory_env) -> None:
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    # 1024-dim deterministic embedding for the round-trip check.
    embedding = [0.1] * 1024

    async with sm() as session, firm_context(firm_id):
        lesson = Lesson(
            firm_id=firm_id,
            text="When a client mentions FBT, always check vehicle logbooks first.",
            category="fbt",
            priority=5,
            embedding=embedding,
        )
        session.add(lesson)
        await session.commit()
        lesson_id = lesson.id

    async with sm() as session, firm_context(firm_id):
        row = (
            await session.execute(
                select(Lesson).where(Lesson.id == lesson_id)
            )
        ).scalar_one()
        assert row.text.startswith("When a client mentions FBT")
        assert row.priority == 5
        assert row.is_active is True
        assert row.embedding is not None
        # pgvector returns a list-like; first element should be 0.1
        assert abs(row.embedding[0] - 0.1) < 1e-6


async def test_entity_and_relationship_with_unique_active_constraint(
    memory_env,
) -> None:
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        director = Entity(
            firm_id=firm_id,
            entity_type="individual",
            name="Alice Director",
        )
        company = Entity(
            firm_id=firm_id,
            entity_type="company",
            name="Acme Pty Ltd",
            abn="12345678901",
        )
        session.add_all([director, company])
        await session.flush()
        rel = EntityRelationship(
            firm_id=firm_id,
            from_entity_id=director.id,
            to_entity_id=company.id,
            relationship_type="director_of",
            provenance={
                "source": "xpm",
                "first_seen": _dt.datetime.now(_dt.UTC).isoformat(),
            },
            confidence=0.95,
        )
        session.add(rel)
        await session.commit()
        director_id, company_id = director.id, company.id

    # A second active edge of the same type between the same pair must fail
    # the unique partial index.
    async with sm() as session, firm_context(firm_id):
        duplicate = EntityRelationship(
            firm_id=firm_id,
            from_entity_id=director_id,
            to_entity_id=company_id,
            relationship_type="director_of",
            provenance={"source": "duplicate"},
        )
        session.add(duplicate)
        from sqlalchemy.exc import IntegrityError

        try:
            await session.commit()
            crashed = False
        except IntegrityError:
            crashed = True
            await session.rollback()
        assert crashed, "duplicate active edge must violate the unique index"

    # But a NEW edge with the same pair becomes possible once the existing
    # one is deactivated.
    async with sm() as session, firm_context(firm_id):
        await session.execute(
            text(
                "UPDATE entity_relationships SET is_active = FALSE "
                "WHERE firm_id = :f"
            ),
            {"f": str(firm_id)},
        )
        await session.commit()
        new_edge = EntityRelationship(
            firm_id=firm_id,
            from_entity_id=director_id,
            to_entity_id=company_id,
            relationship_type="director_of",
            provenance={"source": "re-add"},
        )
        session.add(new_edge)
        await session.commit()  # should succeed now


async def test_entity_relationship_self_loop_is_rejected(memory_env) -> None:
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        person = Entity(
            firm_id=firm_id, entity_type="individual", name="Bob"
        )
        session.add(person)
        await session.flush()
        person_id = person.id
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        loop = EntityRelationship(
            firm_id=firm_id,
            from_entity_id=person_id,
            to_entity_id=person_id,
            relationship_type="spouse_of",
        )
        session.add(loop)
        from sqlalchemy.exc import IntegrityError

        try:
            await session.commit()
            crashed = False
        except IntegrityError:
            crashed = True
            await session.rollback()
        assert crashed, "CHECK constraint must reject self-loops"


async def test_document_tsv_trigger_concatenates_title_summary_body(
    memory_env,
) -> None:
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        doc = Document(
            firm_id=firm_id,
            source="kb",
            doc_type="engagement_letter_template",
            title="Standard Engagement Letter",
            summary="Engagement terms for new clients",
            body="The accountant agrees to perform the following services...",
        )
        session.add(doc)
        await session.commit()
        doc_id = doc.id

    async with sm() as session, firm_context(firm_id):
        tsv = (
            await session.execute(
                text("SELECT tsv::text FROM documents WHERE id = :id"),
                {"id": str(doc_id)},
            )
        ).scalar_one()
        assert "engag" in tsv.lower()  # 'engagement' stem
        assert "client" in tsv.lower()
        assert "account" in tsv.lower()  # 'accountant' stem


async def test_job_and_deadline_inserts_under_firm_context(memory_env) -> None:
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        job = Job(
            firm_id=firm_id,
            xpm_id="xpm-job-1",
            name="FY25 Income Tax Return",
            state="in_progress",
            due_at=_dt.datetime(2025, 10, 31, tzinfo=_dt.UTC),
        )
        deadline = Deadline(
            firm_id=firm_id,
            deadline_type="bas_quarterly",
            title="June 2025 BAS",
            due_at=_dt.datetime(2025, 7, 28, tzinfo=_dt.UTC),
            is_recurring=True,
            recurrence_pattern="quarterly",
        )
        session.add_all([job, deadline])
        await session.commit()
        job_id = job.id

    async with sm() as session, firm_context(firm_id):
        loaded = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one()
        assert loaded.name == "FY25 Income Tax Return"
        assert loaded.state == "in_progress"


async def test_xpm_unique_partial_index_prevents_dup_xpm_job(
    memory_env,
) -> None:
    """Two jobs in the same firm with the same xpm_id violate the unique
    partial index — jobs MUST UPSERT on (firm_id, xpm_id).
    """
    sm = memory_env["sessionmaker"]
    firm_id = await _seed_firm(sm)
    memory_env["created_firm_ids"].append(firm_id)

    async with sm() as session, firm_context(firm_id):
        session.add(
            Job(
                firm_id=firm_id,
                xpm_id="dup-xpm-id",
                name="Job A",
                state="in_progress",
            )
        )
        await session.commit()

    async with sm() as session, firm_context(firm_id):
        session.add(
            Job(
                firm_id=firm_id,
                xpm_id="dup-xpm-id",
                name="Job A duplicate",
                state="in_progress",
            )
        )
        from sqlalchemy.exc import IntegrityError

        try:
            await session.commit()
            crashed = False
        except IntegrityError:
            crashed = True
            await session.rollback()
        assert crashed, "Duplicate (firm_id, xpm_id) must violate the index"
