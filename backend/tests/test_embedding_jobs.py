"""
Tests for the OmniCare embedding-job adapter over ragit's
outbox (ragit plan 05).

The retry/backoff/dead-letter engine itself is tested in
``libs/ragit/tests`` against in-memory doubles; these tests
pin the OmniCare adapter: the ``SqlAlchemyJobStore`` mapping
(claim query, guarded processing transition, completion and
failure updates) and the ``ClaimJobProcessor`` (claim amount
lookup, embedding text, vector-index metadata).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.database import async_session_factory
from app.models.claim import Claim
from app.models.embedding_job import EmbeddingJob
from app.models.user import User
from app.domain.claims.outbox import process_pending_jobs


class _UpsertCapture:
    """Vector-store double that records upserted documents."""

    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def upsert(self, documents, session=None):  # noqa: ANN001
        self.documents.extend(documents)


async def _seed_owner() -> uuid.UUID:
    owner_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(
            User(
                id=owner_id,
                username=f"jobs_{owner_id.hex[:12]}",
                password_hash="not-a-real-hash",  # noqa: S106 - fixture row
            )
        )
        await session.commit()
    return owner_id


async def _seed_claim(owner_id: uuid.UUID, claim_id: str, amount: Decimal) -> None:
    async with async_session_factory() as session:
        session.add(
            Claim(
                claim_id=claim_id,
                policy_number="POL-1092",
                claim_type="Water Damage",
                status="Submitted",
                amount=amount,
                description="Pipe burst causing kitchen flooding.",
                owner_id=owner_id,
            )
        )
        await session.commit()


async def _seed_job(
    owner_id: uuid.UUID,
    claim_id: str,
    *,
    claim_status: str = "Submitted",
    max_attempts: int = 4,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    async with async_session_factory() as session:
        session.add(
            EmbeddingJob(
                id=job_id,
                claim_id=claim_id,
                claim_uuid=uuid.uuid4(),
                owner_id=owner_id,
                claim_type="Water Damage",
                description="Pipe burst causing kitchen flooding.",
                policy_number="POL-1092",
                claim_status=claim_status,
                status="pending",
                status_detail="pending",
                max_attempts=max_attempts,
            )
        )
        await session.commit()
    return job_id


async def _job_row(job_id: uuid.UUID) -> EmbeddingJob:
    async with async_session_factory() as session:
        return await session.get(EmbeddingJob, job_id)


async def _expire_retry_at(job_id: uuid.UUID) -> None:
    """Roll a failed job's retry timestamp into the past."""
    async with async_session_factory() as session:
        job = await session.get(EmbeddingJob, job_id)
        if job.next_retry_at is not None:
            job.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()


def _embed_ok() -> AsyncMock:
    return AsyncMock(return_value=[[0.0] * 1536])


@pytest.mark.asyncio
async def test_process_pending_jobs_completes_and_upserts_claim_index_entry():
    """A pending job is claimed, embedded, upserted, and marked completed."""
    owner_id = await _seed_owner()
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    await _seed_claim(owner_id, claim_id, Decimal("1234.50"))
    job_id = await _seed_job(owner_id, claim_id)

    capture = _UpsertCapture()
    with (
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=_embed_ok(),
        ),
        patch("app.domain.embeddings.get_vector_store", return_value=capture),
    ):
        processed = await process_pending_jobs(limit=10, worker_id="worker-1")

    assert processed == 1
    job = await _job_row(job_id)
    assert job.status == "completed"
    assert job.completed_at is not None
    assert job.locked_by == "worker-1"
    assert job.locked_at is not None

    assert len(capture.documents) == 1
    document = capture.documents[0]
    assert document["text"] == (
        f"Claim {claim_id}: Water Damage - Pipe burst causing kitchen flooding."
    )
    assert document["metadata"]["claim_id"] == claim_id
    assert document["metadata"]["status"] == "Submitted"
    assert document["metadata"]["amount"] == 1234.50
    assert document["metadata"]["owner_id"] == str(owner_id)


@pytest.mark.asyncio
async def test_process_pending_jobs_uses_claim_status_in_metadata():
    """The upserted metadata carries the claim's business status, not the job's."""
    owner_id = await _seed_owner()
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    await _seed_claim(owner_id, claim_id, Decimal("0"))
    job_id = await _seed_job(owner_id, claim_id, claim_status="Denied")

    capture = _UpsertCapture()
    with (
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=_embed_ok(),
        ),
        patch("app.domain.embeddings.get_vector_store", return_value=capture),
    ):
        await process_pending_jobs(limit=10, worker_id="worker-1")

    job = await _job_row(job_id)
    assert job.status == "completed"
    assert capture.documents[0]["metadata"]["status"] == "Denied"


@pytest.mark.asyncio
async def test_process_pending_jobs_defaults_missing_claim_amount_to_zero():
    """A claim row that is already gone still indexes with amount 0.0."""
    owner_id = await _seed_owner()
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    await _seed_job(owner_id, claim_id)

    capture = _UpsertCapture()
    with (
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=_embed_ok(),
        ),
        patch("app.domain.embeddings.get_vector_store", return_value=capture),
    ):
        await process_pending_jobs(limit=10, worker_id="worker-1")

    assert capture.documents[0]["metadata"]["amount"] == 0.0


@pytest.mark.asyncio
async def test_failed_job_is_marked_failed_with_backoff():
    """A failing processor leaves the job failed, retry-due, and unlocked."""
    owner_id = await _seed_owner()
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    await _seed_claim(owner_id, claim_id, Decimal("0"))
    job_id = await _seed_job(owner_id, claim_id)

    embed_fn = AsyncMock(side_effect=Exception("embed error"))
    with (
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=embed_fn,
        ),
        patch("app.domain.embeddings.get_vector_store"),
    ):
        processed = await process_pending_jobs(limit=10, worker_id="worker-1")

    assert processed == 0
    job = await _job_row(job_id)
    assert job.status == "failed"
    assert job.retry_count == 1
    assert job.next_retry_at is not None
    assert job.next_retry_at > datetime.now(UTC)
    assert job.locked_at is None
    assert job.locked_by is None
    assert "embed error" in job.status_detail


@pytest.mark.asyncio
async def test_job_dead_letters_after_exhausting_attempts():
    """A job whose attempts are exhausted moves to the terminal state."""
    owner_id = await _seed_owner()
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    await _seed_claim(owner_id, claim_id, Decimal("0"))
    job_id = await _seed_job(owner_id, claim_id, max_attempts=2)

    embed_fn = AsyncMock(side_effect=Exception("embed error"))
    with (
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=embed_fn,
        ),
        patch("app.domain.embeddings.get_vector_store"),
    ):
        for _ in range(2):
            await process_pending_jobs(limit=10, worker_id="worker-1")
            # The backoff elapses between passes, making the job retry-due.
            await _expire_retry_at(job_id)

    job = await _job_row(job_id)
    assert job.status == "dead_letter"
    assert job.retry_count == 2
    assert job.next_retry_at is None
    assert "Exhausted 2 attempts" in job.status_detail


@pytest.mark.asyncio
async def test_dead_letter_job_is_never_selected():
    """A dead_letter job is not claimable."""
    owner_id = await _seed_owner()
    claim_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"
    job_id = await _seed_job(owner_id, claim_id)
    async with async_session_factory() as session:
        job = await session.get(EmbeddingJob, job_id)
        job.status = "dead_letter"
        job.status_detail = "gave up"
        await session.commit()

    capture = _UpsertCapture()
    with (
        patch(
            "app.domain.embeddings.EmbeddingFactory.get_embedding_function",
            return_value=_embed_ok(),
        ),
        patch("app.domain.embeddings.get_vector_store", return_value=capture),
    ):
        processed = await process_pending_jobs(limit=10, worker_id="worker-1")

    assert processed == 0
    assert capture.documents == []
