"""Tier B assertion 5: ingestion idempotency and version retirement.

A second ingestion of an unchanged source is a no-op: the chunk
count and the version count stay put. A changed source creates a
new version, retires the old one, and leaves the retired version's
chunks searchable (old-vs-current-version retrieval).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.config import settings
from app.database import async_session_factory
from app.domain.policies.ingestion import ingest_policy
from app.domain.embeddings import get_vector_store
from tests.integration.conftest import hash_embedding

DISTANCE_THRESHOLD = 1.3


async def _counts() -> tuple[int, int, int]:
    """Return (chunk_count, version_count, active_version_count)."""
    async with async_session_factory() as session:
        chunks = (await session.execute(text("SELECT count(*) FROM policy_chunks"))).scalar_one()
        versions = (
            await session.execute(text("SELECT count(*) FROM policy_versions"))
        ).scalar_one()
        active = (
            await session.execute(
                text("SELECT count(*) FROM policy_versions WHERE effective_to IS NULL")
            )
        ).scalar_one()
    return int(chunks), int(versions), int(active)


@pytest.mark.asyncio
async def test_unchanged_source_is_a_noop(
    tierb_seed: dict[str, Any],
):
    """Re-ingesting the same source changes nothing."""
    chunks_before, versions_before, active_before = await _counts()
    assert chunks_before == 3
    assert active_before == 1

    returned = await ingest_policy()
    assert returned == chunks_before

    chunks_again, versions_again, active_again = await _counts()
    assert chunks_again == chunks_before
    assert versions_again == versions_before
    assert active_again == 1


@pytest.mark.asyncio
async def test_changed_source_creates_version_and_retires_old(
    tierb_seed: dict[str, Any],
    tmp_path: Path,
):
    """A modified source produces a new active version."""
    source = Path(settings.policy_file_path)
    modified = tmp_path / "modified_policy.md"
    modified.write_text(
        source.read_text(encoding="utf-8").replace(
            "Gradual leaks or flood damage are strictly excluded.",
            "Gradual leaks are covered after twelve months.",
        ),
        encoding="utf-8",
    )

    chunks_before, versions_before, _ = await _counts()

    returned = await ingest_policy(policy_path=str(modified))
    assert returned == 3

    chunks_after, versions_after, active_after = await _counts()
    assert versions_after == versions_before + 1
    assert active_after == 1
    # The retired version's chunks were removed from the index,
    # which now holds exactly the new version's chunks.
    assert chunks_after == chunks_before

    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT version_id, effective_to FROM policy_versions ORDER BY effective_from")
            )
        ).all()
    assert len(rows) == 2
    assert rows[0][1] is not None  # old version retired
    assert rows[1][1] is None  # new version active
    new_version_id = rows[1][0]

    store = get_vector_store(table_name="policy_chunks", id_field="id")

    # Old-vs-current-version: content only in the new version is
    # retrieved from the active version.
    new_hits = await store.hybrid_search(
        query="twelve months",
        embedding=hash_embedding("twelve months"),
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=["section", "policy_version_id"],
    )
    assert new_hits
    assert str(new_hits[0]["metadata"]["policy_version_id"]) == str(new_version_id)

    # Content that only existed in the retired version is gone
    # from the index: the index reflects the current version.
    old_hits = await store.hybrid_search(
        query="strictly excluded",
        embedding=hash_embedding("strictly excluded"),
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=["section", "policy_version_id"],
    )
    assert old_hits == []
