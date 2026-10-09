"""Claims embedding-job outbox adapter over ragit.

The claim embedding job is the OmniCare binding of
ragit's generic outbox: :class:`SqlAlchemyJobStore`
implements ragit's ``EmbeddingJobStore`` protocol
against the ``EmbeddingJob`` model,
:func:`enqueue_embedding_job` writes a pending job row
through :func:`ragit.jobs.enqueue_job` — in the
caller's transaction when a session is supplied, so the
claim row and its job row commit together —
:class:`ClaimJobProcessor` is the ragit
``JobProcessor`` that embeds a claim and upserts its
entry into the claims vector store (the ``claims``
binding lives in :mod:`app.domain.claims.retriever`),
and :func:`process_pending_jobs` /
:func:`reclaim_stale_jobs` wire the store and processor
into ragit's generic job functions.

Invariant for anything that writes an ``embedding_jobs`` row
------------------------------------------------------------
A job row must be committed in the **same transaction** as the claim it refers to,
or written through this outbox by a caller that has already committed that claim.

The reason is that ``embedding_jobs.claim_uuid`` has no foreign key to ``claims``,
so the database will not stop an orphan job from appearing. An enqueue that commits
on its own session races the claim transaction: if the claim transaction rolls back
afterwards, the job survives, points at a claim that does not exist, and the worker
then upserts a claim index entry for a row nobody can ever read.

Therefore:

- ``enqueue_embedding_job(session=...)`` only flushes. Pass the caller's session.
- ``enqueue_embedding_job()`` with no session opens its own and commits. Use it only
  where the claim is already durable.
- ``submit_claim`` adds the ``EmbeddingJob`` to the caller's session directly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.domain.claims.retriever import CLAIMS_RETRIEVER_SPEC
from ragit.jobs import JobPayload, claim_jobs, enqueue_job
from ragit.jobs import (
    process_pending_jobs as _ragit_process_pending_jobs,
    reclaim_stale_jobs as _ragit_reclaim_stale_jobs,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4


def _payload_from_row(row: Any) -> JobPayload:
    """Map an ``EmbeddingJob`` ORM row to a :class:`JobPayload`."""
    return JobPayload(
        job_id=str(row.id),
        payload={
            "claim_uuid": str(row.claim_uuid),
            "claim_id": row.claim_id,
            "owner_id": str(row.owner_id),
            "claim_type": row.claim_type,
            "description": row.description,
            "policy_number": row.policy_number,
            "claim_status": row.claim_status,
        },
        status=row.status,
        status_detail=row.status_detail,
        retry_count=row.retry_count,
        max_attempts=row.max_attempts,
        next_retry_at=row.next_retry_at,
        locked_at=row.locked_at,
        locked_by=row.locked_by,
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


class SqlAlchemyJobStore:
    """ragit ``EmbeddingJobStore`` implemented against ``embedding_jobs``."""

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory or async_session_factory

    async def claim_pending(self, limit: int, _worker_id: str) -> list[JobPayload]:
        """Select claimable jobs with ``FOR UPDATE SKIP LOCKED``.

        ``worker_id`` is part of the ``EmbeddingJobStore`` protocol
        but is not needed here: the lock columns are stamped by
        :func:`claim_jobs` after selection.
        """
        from sqlalchemy import select  # noqa: PLC0415

        from app.models.embedding_job import EmbeddingJob  # noqa: PLC0415

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                select(EmbeddingJob)
                .where(
                    (EmbeddingJob.status == "pending")
                    | (
                        (EmbeddingJob.status == "failed")
                        & (EmbeddingJob.next_retry_at <= now)
                        & (EmbeddingJob.retry_count < EmbeddingJob.max_attempts)
                    )
                )
                .order_by(EmbeddingJob.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            rows = result.scalars().all()
        return [_payload_from_row(row) for row in rows]

    async def mark_processing(self, jobs: list[JobPayload], worker_id: str) -> list[JobPayload]:
        """Transition jobs to ``processing`` and stamp the lock.

        The guarded update (``status IN ('pending', 'failed')``) keeps
        two concurrent drainers from claiming the same row: only the
        worker whose update matches still owns the job.
        """
        from sqlalchemy import update  # noqa: PLC0415

        from app.models.embedding_job import EmbeddingJob  # noqa: PLC0415

        if not jobs:
            return []

        now = datetime.now(UTC)
        claim_jobs(jobs, worker_id, now)

        claimed: list[JobPayload] = []
        async with self._session_factory() as session:
            for job in jobs:
                result = await session.execute(
                    update(EmbeddingJob)
                    .where(
                        EmbeddingJob.id == uuid.UUID(str(job.job_id)),
                        EmbeddingJob.status.in_(["pending", "failed"]),
                    )
                    .values(
                        status=job.status,
                        status_detail=job.status_detail,
                        next_retry_at=job.next_retry_at,
                        locked_at=job.locked_at,
                        locked_by=job.locked_by,
                    )
                    .returning(EmbeddingJob.id)
                )
                if result.scalar() is not None:
                    claimed.append(job)
            await session.commit()
        return claimed

    async def mark_completed(self, job: JobPayload) -> None:
        """Persist a successfully processed job as ``completed``."""
        from sqlalchemy import update  # noqa: PLC0415

        from app.models.embedding_job import EmbeddingJob  # noqa: PLC0415

        async with self._session_factory() as session:
            await session.execute(
                update(EmbeddingJob)
                .where(EmbeddingJob.id == uuid.UUID(str(job.job_id)))
                .values(status=job.status, completed_at=job.completed_at)
            )
            await session.commit()

    async def mark_failed(self, job: JobPayload, error: Exception) -> None:
        """Persist a failed job's retry/dead-letter transition."""
        from sqlalchemy import update  # noqa: PLC0415

        from app.models.embedding_job import EmbeddingJob  # noqa: PLC0415

        async with self._session_factory() as session:
            await session.execute(
                update(EmbeddingJob)
                .where(EmbeddingJob.id == uuid.UUID(str(job.job_id)))
                .values(
                    status=job.status,
                    status_detail=job.status_detail,
                    retry_count=job.retry_count,
                    next_retry_at=job.next_retry_at,
                    locked_at=job.locked_at,
                    locked_by=job.locked_by,
                )
            )
            await session.commit()

    async def reclaim_stale(self, stale_after_seconds: float | None = None) -> int:
        """Reset jobs abandoned in ``processing`` back to ``pending``."""
        from sqlalchemy import select  # noqa: PLC0415

        from app.models.embedding_job import EmbeddingJob  # noqa: PLC0415

        if stale_after_seconds is None:
            from app.config import settings  # noqa: PLC0415

            stale_after_seconds = settings.job_stale_after_seconds

        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        reclaimed = 0
        async with self._session_factory() as session:
            result = await session.execute(
                select(EmbeddingJob)
                .where(
                    (EmbeddingJob.status == "processing")
                    & (EmbeddingJob.locked_at.is_(None) | (EmbeddingJob.locked_at <= cutoff))
                )
                .with_for_update(skip_locked=True)
            )
            stale_jobs = result.scalars().all()
            for job in stale_jobs:
                job.status = "pending"
                job.status_detail = None
                job.locked_at = None
                job.locked_by = None
                reclaimed += 1
            if reclaimed:
                await session.commit()

        if reclaimed:
            logger.info("Reclaimed %d stale embedding job(s).", reclaimed)
        return reclaimed

    async def enqueue(self, payload: dict[str, Any], session: Any = None) -> None:
        """Write a new pending ``embedding_jobs`` row.

        When ``session`` is supplied the row is only added and flushed;
        the caller owns the commit, so the job and the claim land in one
        transaction. Without a session this opens its own session and
        commits.
        """
        from app.models.embedding_job import EmbeddingJob  # noqa: PLC0415

        job = EmbeddingJob(
            claim_uuid=uuid.UUID(str(payload["claim_uuid"])),
            claim_id=payload["claim_id"],
            owner_id=uuid.UUID(str(payload["owner_id"])),
            claim_type=payload["claim_type"],
            description=payload["description"],
            policy_number=payload["policy_number"],
            claim_status=payload["claim_status"],
            status="pending",
            status_detail="pending",
        )
        if session is not None:
            session.add(job)
            await session.flush()
        else:
            async with self._session_factory() as owned_session:
                owned_session.add(job)
                await owned_session.commit()


class ClaimJobProcessor:
    """ragit ``JobProcessor`` that embeds a claim and upserts its index entry."""

    async def process(self, job: JobPayload) -> None:
        """Embed the claim text and upsert it into the claims vector store."""
        from sqlalchemy import select  # noqa: PLC0415

        from app.config import get_settings  # noqa: PLC0415
        from app.domain.embeddings import EmbeddingFactory, get_vector_store  # noqa: PLC0415
        from app.models.claim import Claim  # noqa: PLC0415
        from ragit.embeddings import get_embedding_dimension  # noqa: PLC0415
        from ragit.validation import validate_embedding  # noqa: PLC0415

        payload = job.payload
        claim_id = payload["claim_id"]

        async with async_session_factory() as session:
            claim_amount = await session.scalar(
                select(Claim.amount).where(Claim.claim_id == claim_id)
            )
        claim_amount = claim_amount if claim_amount is not None else 0.0

        embed_fn = EmbeddingFactory.get_embedding_function()
        text_content = f"Claim {claim_id}: {payload['claim_type']} - {payload['description']}"
        embeddings = await embed_fn([text_content])
        embedding = embeddings[0]
        validate_embedding(
            embedding,
            expected_dim=get_embedding_dimension(get_settings().embedding_model),
            label="claim embedding",
        )

        store = get_vector_store(
            table_name=CLAIMS_RETRIEVER_SPEC.table_name,
            id_field=CLAIMS_RETRIEVER_SPEC.id_field,
        )
        await store.upsert(
            documents=[
                {
                    "id": payload["claim_uuid"],
                    "text": text_content,
                    "embedding": embedding,
                    "metadata": {
                        "claim_id": claim_id,
                        "policy_number": payload["policy_number"],
                        "claim_type": payload["claim_type"],
                        "status": payload["claim_status"],
                        "amount": float(claim_amount),
                        "description": payload["description"],
                        "owner_id": payload["owner_id"],
                    },
                }
            ]
        )


async def enqueue_embedding_job(  # noqa: PLR0913, PLR0917
    claim_uuid: uuid.UUID,
    claim_id: str,
    owner_id: uuid.UUID,
    claim_type: str,
    description: str,
    policy_number: str,
    claim_status: str,
    session: Any = None,
) -> None:
    """Add an embedding job for a newly submitted claim.

    Delegates to ragit's :func:`ragit.jobs.enqueue_job`
    with the caller's session: when ``session`` is supplied,
    the job row is only added and flushed, so the claim and
    its embedding job are persisted atomically by the
    caller's commit. When ``session`` is None, ragit opens
    and commits its own transaction.

    Args:
        claim_uuid: Internal UUID of the claim row.
        claim_id: Human-readable claim identifier.
        owner_id: UUID of the user who owns the claim.
        claim_type: Category of the claim.
        description: Factual description of the incident.
        policy_number: The policyholder's policy number.
        claim_status: Current processing status.
        session: Optional existing database session. When
            provided, the job row joins the caller's
            transaction and the caller commits.
    """
    store = SqlAlchemyJobStore()
    payload = {
        "claim_uuid": str(claim_uuid),
        "claim_id": claim_id,
        "owner_id": str(owner_id),
        "claim_type": claim_type,
        "description": description,
        "policy_number": policy_number,
        "claim_status": claim_status,
    }
    try:
        await enqueue_job(store, payload, session=session)
        logger.info("Enqueued embedding job for claim '%s'.", claim_id)
    except Exception as exc:
        logger.exception("Failed to enqueue embedding job for claim '%s': %s", claim_id, exc)


async def enqueue_claim_embedding_job(  # noqa: PLR0913, PLR0917
    claim_uuid: uuid.UUID,
    claim_id: str,
    owner_id: uuid.UUID,
    claim_type: str,
    description: str,
    policy_number: str,
    claim_status: str,
    session: AsyncSession | None = None,
) -> None:
    """Add an embedding job for a newly submitted claim.

    OmniCare-named alias of :func:`enqueue_embedding_job`;
    see that function for the same-transaction semantics.
    """
    await enqueue_embedding_job(
        claim_uuid,
        claim_id,
        owner_id,
        claim_type,
        description,
        policy_number,
        claim_status,
        session=session,
    )


async def process_pending_jobs(limit: int = 10, worker_id: str | None = None) -> int:
    """Process pending embedding jobs.

    Fetches up to ``limit`` pending jobs, generates embeddings, and upserts
    them into the vector store. Failed jobs are marked with an error detail
    for retry.

    Claimed jobs are stamped with ``locked_at``/``locked_by`` for the whole
    processing transaction. A worker that dies mid-pass therefore leaves a
    visible lock, which :func:`reclaim_stale_jobs` later resets.

    Args:
        limit: Maximum number of jobs to process in one batch.
        worker_id: Identity stamped into ``locked_by``. Defaults to
            ``settings.worker_id``.

    Returns:
        int: Number of jobs successfully processed.
    """
    from app.config import settings  # noqa: PLC0415

    if worker_id is None:
        worker_id = settings.worker_id

    store = SqlAlchemyJobStore()
    processor = ClaimJobProcessor()
    return await _ragit_process_pending_jobs(
        store,
        processor,
        limit=limit,
        worker_id=worker_id,
    )


async def reclaim_stale_jobs(stale_after_seconds: float | None = None) -> int:
    """Reset jobs abandoned in ``processing`` back to ``pending``.

    A drainer that dies mid-upsert leaves its rows locked in
    ``processing`` forever: the claim query only selects ``pending``
    or retry-due ``failed`` rows, so no live worker can pick them up
    again. This reclaims rows whose ``locked_at`` is older than
    ``stale_after_seconds`` — or whose lock timestamp is NULL, which
    marks rows locked before the column existed — so a replacement
    worker can process them.

    Args:
        stale_after_seconds: Lock age in seconds that counts as
            abandoned. Defaults to ``settings.job_stale_after_seconds``.

    Returns:
        int: Number of jobs reclaimed.
    """
    if stale_after_seconds is None:
        from app.config import settings  # noqa: PLC0415

        stale_after_seconds = settings.job_stale_after_seconds

    store = SqlAlchemyJobStore()
    return await _ragit_reclaim_stale_jobs(
        store,
        stale_after_seconds=stale_after_seconds,
    )


__all__ = [
    "MAX_ATTEMPTS",
    "ClaimJobProcessor",
    "SqlAlchemyJobStore",
    "enqueue_claim_embedding_job",
    "enqueue_embedding_job",
    "process_pending_jobs",
    "reclaim_stale_jobs",
]
