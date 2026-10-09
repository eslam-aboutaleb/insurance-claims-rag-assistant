"""
Tests for versioned policy ingestion (Feature 15: document-version-model).

The ingestion orchestration and the snapshot comparison that makes ingestion idempotent live
in the shared ``ragit`` library (tested in ``libs/ragit/tests/test_ingestion_pipeline.py``).
These tests exercise the OmniCare ``PolicyVersionStore`` -- the ``VersionStore`` implementation
the adapter binds to the pipeline -- against the real test database.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session_factory
from app.domain.policies.ingestion import PolicyVersionStore
from ragit.ingestion import IngestionSnapshot

_POLICY_TABLES = ("policy_chunks", "policy_versions", "policy_ingestion_meta", "policies")


@pytest_asyncio.fixture
async def clean_policy_tables():
    """Delete policy rows before and after the test."""

    async def _clear() -> None:
        async with async_session_factory() as session:
            for table in _POLICY_TABLES:
                # Table names come from the fixed tuple above, never from test input.
                await session.execute(text(f'DELETE FROM "{table}"'))  # noqa: S608
            await session.commit()

    await _clear()
    yield
    await _clear()


def test_policy_version_effective_from_and_source_hash_non_null():
    """PolicyVersion requires effective_from and source_hash non-null at the ORM level."""
    from app.models.policy_version import PolicyVersion as PV

    assert "effective_from" in PV.__table__.c
    assert "source_hash" in PV.__table__.c
    assert PV.__table__.c["effective_from"].nullable is False
    assert PV.__table__.c["source_hash"].nullable is False


def _snapshot(source_hash: str) -> IngestionSnapshot:
    return IngestionSnapshot(
        source_hash=source_hash,
        embedding_model="text-embedding-3-small",
        embedding_dim=1536,
        chunker_version="v1",
        chunk_size=600,
        overlap=100,
        retrieval_schema_version="v1",
    )


@pytest.mark.asyncio
async def test_store_creates_policy_and_version_when_missing(clean_policy_tables):
    """create_version() creates the Policy and PolicyVersion when none exist."""
    store = PolicyVersionStore()

    async with async_session_factory() as session:
        record = await store.create_version(session, _snapshot("hash-1"))
        await session.commit()

        policies = (
            (await session.execute(text("SELECT product, jurisdiction FROM policies")))
            .mappings()
            .all()
        )
        versions = (
            (
                await session.execute(
                    text(
                        "SELECT source_hash, embedding_model, embedding_dim, "
                        "chunker_version, chunk_size, overlap, retrieval_schema_version "
                        "FROM policy_versions"
                    )
                )
            )
            .mappings()
            .all()
        )

    assert policies == [{"product": "omnicare_base", "jurisdiction": "US"}]
    assert len(versions) == 1
    version = versions[0]
    assert version["source_hash"] == "hash-1"
    assert version["embedding_model"] == "text-embedding-3-small"
    assert version["embedding_dim"] == 1536
    assert version["chunker_version"] == "v1"
    assert version["chunk_size"] == 600
    assert version["overlap"] == 100
    assert version["retrieval_schema_version"] == "v1"
    assert record.version_id
    assert record.source_id


@pytest.mark.asyncio
async def test_store_find_active_returns_the_stored_snapshot(clean_policy_tables):
    """find_active() returns the active version's snapshot.

    The pipeline compares this snapshot against the current one
    to decide whether the source changed (the "skip when the
    hash is unchanged" decision); the comparison itself is
    tested in ragit.
    """
    store = PolicyVersionStore()

    async with async_session_factory() as session:
        created = await store.create_version(session, _snapshot("hash-1"))
        await session.commit()

        active = await store.find_active(session)

    assert active is not None
    assert active.version_id == created.version_id
    assert active.source_id == created.source_id
    assert active.snapshot.source_hash == "hash-1"
    assert active.snapshot.embedding_model == "text-embedding-3-small"
    assert active.snapshot.embedding_dim == 1536
    assert active.snapshot.chunker_version == "v1"
    assert active.snapshot.chunk_size == 600
    assert active.snapshot.overlap == 100
    assert active.snapshot.retrieval_schema_version == "v1"


@pytest.mark.asyncio
async def test_store_closes_old_version_and_creates_new(clean_policy_tables):
    """close_active() retires the old version; create_version() adds a new active one."""
    store = PolicyVersionStore()

    async with async_session_factory() as session:
        old = await store.create_version(session, _snapshot("hash-old"))
        await store.close_active(session)
        new = await store.create_version(session, _snapshot("hash-new"))
        await session.commit()

        rows = (
            (
                await session.execute(
                    text(
                        "SELECT version_id, source_hash, effective_to "
                        "FROM policy_versions ORDER BY effective_from"
                    )
                )
            )
            .mappings()
            .all()
        )

    assert len(rows) == 2
    by_hash = {row["source_hash"]: row for row in rows}
    assert by_hash["hash-old"]["effective_to"] is not None
    assert by_hash["hash-new"]["effective_to"] is None
    assert str(by_hash["hash-old"]["version_id"]) == old.version_id
    assert str(by_hash["hash-new"]["version_id"]) == new.version_id
