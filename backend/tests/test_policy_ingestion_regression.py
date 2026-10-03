"""
Regression tests for policy ingestion against the real PostgreSQL and pgvector.

Three defects made ingestion fail outright in a deployed container, each only visible
when the pipeline actually ran end to end:

1. ``policy_versions.embedding_dim`` (and ``chunk_size``, ``overlap``) were declared as
   ``Mapped[int | None]`` but mapped to a ``Text`` column. SQLAlchemy emitted
   ``$n::VARCHAR``, asyncpg rejected the integer, and every INSERT into ``policy_versions``
   failed. Covered by test that the columns are integers.

2. The advisory-lock release ran in a ``finally`` without a preceding rollback. A failed
   statement left the session in an aborted transaction, so ``pg_advisory_unlock`` raised
   ``InFailedSQLTransactionError`` and replaced the real ingestion error with one about
   the lock. The rollback-before-unlock now lives in ragkit's ``advisory_lock`` context
   manager (tested in ``libs/ragkit/tests/test_ingestion_locking.py``); the adapter test
   below verifies the originating error still reaches the caller.

3. ``policy_id`` and ``policy_version_id`` were attached to the chunk dict at the top
   level, but the vector store derives its INSERT column list from
   ``document["metadata"]`` only. The columns were silently dropped and the insert failed
   on the ``policy_chunks.policy_id`` not-null constraint.

These tests use the live database. They truncate the policy tables only, so they can run
alongside the rest of the suite.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import inspect as sa_inspect, text, types as sa_types

from app.database import engine

_POLICY_TABLES = ("policy_chunks", "policy_versions", "policy_ingestion_meta", "policies")


@pytest_asyncio.fixture
async def clean_policy_tables():
    """Delete policy rows before and after the test."""
    from app.database import async_session_factory

    async def _clear() -> None:
        async with async_session_factory() as session:
            for table in _POLICY_TABLES:
                # Table names come from the fixed tuple above, never from test input.
                await session.execute(text(f'DELETE FROM "{table}"'))  # noqa: S608
            await session.commit()

    await _clear()
    yield
    await _clear()


@pytest.mark.asyncio
async def test_policy_version_numeric_columns_are_integers() -> None:
    """The numeric metadata columns must be integer, not text.

    An integer value in a text column is rejected by asyncpg because SQLAlchemy casts
    the bind parameter to ``VARCHAR``, which aborts ingestion before any chunk is
    written.
    """

    def _inspect(connection) -> dict[str, str]:
        inspector = sa_inspect(connection)
        return {
            column["name"]: column["type"] for column in inspector.get_columns("policy_versions")
        }

    async with engine.connect() as connection:
        types = await connection.run_sync(_inspect)

    for name in ("embedding_dim", "chunk_size", "overlap"):
        assert isinstance(types[name], sa_types.Integer), (
            f"policy_versions.{name} is {types[name]!r}; ingestion writes an int into it"
        )


@pytest.mark.asyncio
async def test_ingest_writes_policy_chunks_with_ownership(clean_policy_tables) -> None:
    """Ingestion stores chunks with the policy and version they belong to."""
    import asyncio

    from app.rag.ingest import ingest_policy

    count = await ingest_policy()
    assert count > 0, "ingestion produced no chunks"

    from app.database import async_session_factory

    async def _read() -> list[tuple]:
        async with async_session_factory() as session:
            rows = await session.execute(
                text(
                    "SELECT c.chunk_id, c.policy_id IS NOT NULL, "
                    "c.policy_version_id IS NOT NULL, c.embedding IS NOT NULL "
                    "FROM policy_chunks c"
                )
            )
            return list(rows)

    rows = await _read()
    assert len(rows) == count
    for chunk_id, has_policy, has_version, has_embedding in rows:
        assert has_policy, f"chunk {chunk_id} has no policy_id"
        assert has_version, f"chunk {chunk_id} has no policy_version_id"
        assert has_embedding, f"chunk {chunk_id} has no embedding"
    assert asyncio.get_event_loop_policy() is not None


@pytest.mark.asyncio
async def test_ingest_is_idempotent(clean_policy_tables) -> None:
    """Re-running ingestion with an unchanged document does not duplicate chunks."""
    from app.rag.ingest import ingest_policy

    first = await ingest_policy()
    second = await ingest_policy()

    assert first == second

    from app.database import async_session_factory

    async def _count() -> int:
        async with async_session_factory() as session:
            rows = await session.execute(text("SELECT count(*) FROM policy_chunks"))
            return rows.scalar_one()

    assert await _count() == first


@pytest.mark.asyncio
async def test_ingest_surfaces_the_real_error_not_the_lock_cleanup() -> None:
    """A failing ingestion must report the original error, not a lock-cleanup error.

    Regression test for the missing rollback before ``pg_advisory_unlock``.

    The rollback-before-unlock now lives in ragkit's ``advisory_lock``
    context manager (tested in ``libs/ragkit/tests/test_ingestion_locking.py``),
    so a failed statement on the pipeline's owned session is rolled back before
    the lock is released. This test verifies the adapter propagates the
    pipeline's original error to the caller unchanged, instead of replacing it
    with a lock-cleanup error such as "current transaction is aborted".
    """
    from sqlalchemy.exc import DBAPIError

    from app.rag import ingest as ingest_module

    real_error = DBAPIError(
        "INSERT INTO policy_versions (policy_id) VALUES (gen_random_uuid())",
        {},
        Exception("policy_versions_policy_id_fkey"),
    )

    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock(side_effect=real_error)
    mock_source = MagicMock()

    with (
        patch.object(ingest_module, "IngestionPipeline", return_value=mock_pipeline),
        patch.object(ingest_module, "FileDocumentSource", return_value=mock_source),
        patch.object(ingest_module, "get_vector_store"),
        patch.object(ingest_module, "EmbeddingFactory"),
    ):
        with pytest.raises(DBAPIError) as excinfo:
            await ingest_module.ingest_policy()

    message = str(excinfo.value)
    # The originating constraint violation reaches the caller unchanged...
    assert excinfo.value is real_error, (
        "expected the originating error to reach the caller, got: " + message
    )
    assert "policy_versions_policy_id_fkey" in message, (
        "expected the originating constraint violation to reach the caller, got: " + message
    )
    # ...not a lock-cleanup error from an aborted transaction.
    assert "current transaction is aborted" not in message, (
        "the advisory-lock cleanup masked the real ingestion error: " + message
    )
