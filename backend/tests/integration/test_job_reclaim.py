"""Tier B: stale-job reclaim for the embedding outbox.

A drainer that dies mid-upsert leaves its rows locked in
``processing`` forever. ``reclaim_stale_jobs`` must reset exactly
the abandoned rows -- old lock timestamp, or a NULL lock from
before the column existed -- and leave live locks and other
statuses alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.claims.outbox import reclaim_stale_jobs


async def _insert_job(
    owner_id: uuid.UUID,
    *,
    status: str,
    locked_at: datetime | None,
    locked_by: str | None,
) -> str:
    from app.database import async_session_factory
    from app.models.embedding_job import EmbeddingJob

    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    async with async_session_factory() as session:
        session.add(
            EmbeddingJob(
                claim_id=claim_id,
                claim_uuid=uuid.uuid4(),
                owner_id=owner_id,
                claim_type="Water Damage",
                description="Pipe burst causing kitchen flooding.",
                policy_number="POL-1092",
                claim_status="Submitted",
                status=status,
                status_detail=None,
                locked_at=locked_at,
                locked_by=locked_by,
            )
        )
        await session.commit()
    return claim_id


async def _job_states() -> dict[str, tuple[str, str | None, datetime | None]]:
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.embedding_job import EmbeddingJob

    async with async_session_factory() as session:
        rows = await session.execute(
            select(
                EmbeddingJob.claim_id,
                EmbeddingJob.status,
                EmbeddingJob.locked_by,
                EmbeddingJob.locked_at,
            )
        )
        return {
            claim_id: (status, locked_by, locked_at)
            for claim_id, status, locked_by, locked_at in rows.all()
        }


@pytest.mark.asyncio
async def test_reclaim_resets_only_abandoned_locks(
    seeded_users: dict[str, uuid.UUID],
):
    """Stale and NULL-locked processing rows return to pending; live locks stay."""
    owner = seeded_users["a"]
    stale_id = await _insert_job(
        owner,
        status="processing",
        locked_at=datetime.now(UTC) - timedelta(hours=1),
        locked_by="dead-worker",
    )
    null_lock_id = await _insert_job(
        owner,
        status="processing",
        locked_at=None,
        locked_by="unknown",
    )
    live_id = await _insert_job(
        owner,
        status="processing",
        locked_at=datetime.now(UTC),
        locked_by="live-worker",
    )
    pending_id = await _insert_job(
        owner,
        status="pending",
        locked_at=None,
        locked_by=None,
    )

    reclaimed = await reclaim_stale_jobs(stale_after_seconds=300)

    assert reclaimed == 2
    states = await _job_states()

    assert states[stale_id][0] == "pending"
    assert states[stale_id][1] is None
    assert states[stale_id][2] is None
    assert states[null_lock_id][0] == "pending"
    assert states[null_lock_id][1] is None
    assert states[live_id] == ("processing", "live-worker", states[live_id][2])
    assert states[live_id][2] is not None
    assert states[pending_id][0] == "pending"


@pytest.mark.asyncio
async def test_reclaim_with_no_stale_jobs_is_a_no_op(
    seeded_users: dict[str, uuid.UUID],
):
    """A queue with only live locks reclaims nothing and commits nothing."""
    owner = seeded_users["a"]
    live_id = await _insert_job(
        owner,
        status="processing",
        locked_at=datetime.now(UTC),
        locked_by="live-worker",
    )

    reclaimed = await reclaim_stale_jobs(stale_after_seconds=300)

    assert reclaimed == 0
    states = await _job_states()
    assert states[live_id][0] == "processing"
    assert states[live_id][1] == "live-worker"
    assert states[live_id][2] is not None


@pytest.mark.asyncio
async def test_reclaim_uses_the_configured_default_threshold(
    seeded_users: dict[str, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
):
    """The default threshold comes from settings, not a hardcoded constant."""
    from app.config import settings

    owner = seeded_users["a"]
    await _insert_job(
        owner,
        status="processing",
        locked_at=datetime.now(UTC) - timedelta(seconds=settings.job_stale_after_seconds + 1),
        locked_by="dead-worker",
    )

    reclaimed = await reclaim_stale_jobs()

    assert reclaimed == 1
