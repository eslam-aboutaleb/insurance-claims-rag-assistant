"""Domain-level policy ingestion tests (ragkit plan 06 T4b).

The ``chunker_version`` trap: the stored snapshot carries
``chunker_version="v1"`` so an unchanged document compares
equal against the active version and a second run creates no
new ``policy_versions`` row and ingests no new chunks.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session_factory

_POLICY_TABLES = ("policy_chunks", "policy_versions", "policy_ingestion_meta", "policies")


@pytest_asyncio.fixture
async def clean_policy_tables():
    """Delete policy rows before and after the test (dedicated test DB only)."""

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
async def test_policy_ingestion_is_idempotent(clean_policy_tables):
    """Second run creates no new policy_versions row (the chunker_version trap)."""
    from app.domain.policies.ingestion import ingest_policy

    first = await ingest_policy()
    assert first > 0

    async with async_session_factory() as session:
        versions_after_first = (
            await session.execute(text("SELECT count(*) FROM policy_versions"))
        ).scalar_one()
        chunks_after_first = (
            await session.execute(text("SELECT count(*) FROM policy_chunks"))
        ).scalar_one()

    second = await ingest_policy()

    async with async_session_factory() as session:
        versions_after_second = (
            await session.execute(text("SELECT count(*) FROM policy_versions"))
        ).scalar_one()
        chunks_after_second = (
            await session.execute(text("SELECT count(*) FROM policy_chunks"))
        ).scalar_one()

    assert second == first
    assert versions_after_second == versions_after_first
    assert chunks_after_second == chunks_after_first
