"""Notification outbox over ragnor's generic job store.

Claimant-facing notifications follow the same protocol as the claim embedding
jobs. :class:`NotificationJobStore` implements ragnor's ``EmbeddingJobStore``
protocol against the ``notification_jobs`` table,
:func:`enqueue_notification_job` writes a pending row through
:func:`ragnor.jobs.enqueue_job` — in the caller's transaction when a session is
supplied, so the claim change and the notification about it commit together —
:class:`NotificationJobProcessor` renders the message from a per-job-type template
and materialises it in the ``notifications`` feed, and
:func:`process_pending_notification_jobs` /
:func:`reclaim_stale_notification_jobs` wire the store and processor into ragnor's
generic job functions.

Invariant for anything that writes a ``notification_jobs`` row
----------------------------------------------------------------
A notification job must be committed in the **same transaction** as the claim
change it describes, or written through this outbox by a caller that has already
committed that change.

The reason is delivery, not referential integrity: a job row that survives a
rolled-back status change notifies a policyholder about a transition that never
happened, and there is no way to take it back once it has been read. The claim
change is the fact; the notification is a promise about it, so the two land
together or neither lands.

Therefore:

- ``enqueue_notification_job(session=...)`` only flushes. Pass the caller's session.
- ``enqueue_notification_job()`` with no session opens its own and commits. Use it
  only where the claim change is already durable.

Delivery is never inline
------------------------
No request thread calls a provider. The API writes the job row; the drain worker
in :mod:`app.workers.notification_drain` delivers it. That keeps a slow or failing
provider off the claim lifecycle endpoints and makes the notification retryable.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.models.platform.notification_job import (
    CHANNEL_IN_APP,
    JOB_TYPE_CLAIM_STATUS_CHANGED,
    JOB_TYPE_CLAIM_SUBMITTED,
    JOB_TYPE_CLAIM_WITHDRAWN,
    MAX_ATTEMPTS,
    NOTIFICATION_JOB_TYPES,
    Notification,
    NotificationJob,
)
from ragnor.jobs import JobPayload, claim_jobs, enqueue_job
from ragnor.jobs import (
    process_pending_jobs as _ragnor_process_pending_jobs,
    reclaim_stale_jobs as _ragnor_reclaim_stale_jobs,
)

logger = logging.getLogger(__name__)


def _payload_from_row(row: NotificationJob) -> JobPayload:
    """Map a ``NotificationJob`` ORM row to a :class:`JobPayload`."""
    return JobPayload(
        job_id=str(row.id),
        payload=dict(row.payload or {}),
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


class NotificationJobStore:
    """ragnor ``EmbeddingJobStore`` implemented against ``notification_jobs``."""

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory or async_session_factory

    async def claim_pending(self, limit: int, _worker_id: str) -> list[JobPayload]:
        """Select claimable jobs with ``FOR UPDATE SKIP LOCKED``.

        ``worker_id`` is part of the ``EmbeddingJobStore`` protocol
        but is not needed here: the lock columns are stamped by
        :func:`claim_jobs` after selection.
        """
        from sqlalchemy import select  # noqa: PLC0415

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                select(NotificationJob)
                .where(
                    (NotificationJob.status == "pending")
                    | (
                        (NotificationJob.status == "failed")
                        & (NotificationJob.next_retry_at <= now)
                        & (NotificationJob.retry_count < NotificationJob.max_attempts)
                    )
                )
                .order_by(NotificationJob.created_at)
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

        if not jobs:
            return []

        now = datetime.now(UTC)
        claim_jobs(jobs, worker_id, now)

        claimed: list[JobPayload] = []
        async with self._session_factory() as session:
            for job in jobs:
                result = await session.execute(
                    update(NotificationJob)
                    .where(
                        NotificationJob.id == uuid.UUID(str(job.job_id)),
                        NotificationJob.status.in_(["pending", "failed"]),
                    )
                    .values(
                        status=job.status,
                        status_detail=job.status_detail,
                        next_retry_at=job.next_retry_at,
                        locked_at=job.locked_at,
                        locked_by=job.locked_by,
                    )
                    .returning(NotificationJob.id)
                )
                if result.scalar() is not None:
                    claimed.append(job)
            await session.commit()
        return claimed

    async def mark_completed(self, job: JobPayload) -> None:
        """Persist a successfully processed job as ``completed``."""
        from sqlalchemy import update  # noqa: PLC0415

        async with self._session_factory() as session:
            await session.execute(
                update(NotificationJob)
                .where(NotificationJob.id == uuid.UUID(str(job.job_id)))
                .values(status=job.status, completed_at=job.completed_at)
            )
            await session.commit()

    async def mark_failed(self, job: JobPayload, error: Exception) -> None:
        """Persist a failed job's retry/dead-letter transition."""
        from sqlalchemy import update  # noqa: PLC0415

        async with self._session_factory() as session:
            await session.execute(
                update(NotificationJob)
                .where(NotificationJob.id == uuid.UUID(str(job.job_id)))
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

        if stale_after_seconds is None:
            from app.core.config import settings  # noqa: PLC0415

            stale_after_seconds = settings.job_stale_after_seconds

        cutoff = datetime.now(UTC) - timedelta(seconds=stale_after_seconds)
        reclaimed = 0
        async with self._session_factory() as session:
            result = await session.execute(
                select(NotificationJob)
                .where(
                    (NotificationJob.status == "processing")
                    & (NotificationJob.locked_at.is_(None) | (NotificationJob.locked_at <= cutoff))
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
            logger.info("Reclaimed %d stale notification job(s).", reclaimed)
        return reclaimed

    async def enqueue(self, payload: dict[str, Any], session: Any = None) -> None:
        """Write a new pending ``notification_jobs`` row.

        When ``session`` is supplied the row is only added and flushed;
        the caller owns the commit, so the job and the claim change it
        describes land in one transaction. Without a session this opens
        its own session and commits.

        ``payload`` carries the addressing (``job_type``, ``user_id``,
        ``channel``, ``max_attempts``) alongside the job's own ``data``,
        which is what the processor's templates read back.
        """
        job = NotificationJob(
            user_id=uuid.UUID(str(payload["user_id"])),
            job_type=str(payload["job_type"]),
            payload=dict(payload.get("data") or {}),
            channel=str(payload.get("channel") or CHANNEL_IN_APP),
            status="pending",
            status_detail="pending",
            max_attempts=int(payload.get("max_attempts") or MAX_ATTEMPTS),
        )
        if session is not None:
            session.add(job)
            await session.flush()
        else:
            async with self._session_factory() as owned_session:
                owned_session.add(job)
                await owned_session.commit()


class NotificationJobProcessor:
    """ragnor ``JobProcessor`` that renders a notification and delivers it."""

    async def process(self, job: JobPayload) -> None:
        """Render and deliver one claimed notification job.

        The row is re-read rather than trusting the payload alone, because
        ``job_type`` and ``channel`` are columns on the outbox and the payload is
        the business data the templates read. A row that has since been deleted
        fails the attempt instead of delivering a notification about nothing.

        Args:
            job: The claimed job.

        Raises:
            ValueError: The outbox row is gone, or its ``job_type`` is not one the
                templates can render.
        """
        async with async_session_factory() as session:
            row = await session.get(NotificationJob, uuid.UUID(str(job.job_id)))
            if row is None:
                raise ValueError(f"Notification job '{job.job_id}' no longer exists.")
            job_type = row.job_type
            channel = row.channel
            payload = dict(row.payload or {})
            user_id = row.user_id

        if job_type not in NOTIFICATION_JOB_TYPES:
            raise ValueError(f"Unknown notification job type '{job_type}'.")

        title, body = render_notification(job_type, payload)
        await deliver_notification(
            job_id=uuid.UUID(str(job.job_id)),
            user_id=user_id,
            job_type=job_type,
            channel=channel,
            payload=payload,
            title=title,
            body=body,
        )


async def _deliver_in_app(  # noqa: PLR0913
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    job_type: str,
    title: str,
    body: str,
    claim_id: str | None,
) -> None:
    """Materialise a delivered notification in the owner's feed.

    Written outside the claim transaction, which is the point: a claimant must not
    wait on a feed write to move a claim, and a feed write must not be able to
    roll back a status change. The unique key on ``job_id`` makes the write
    idempotent, so a redelivery after a reclaimed lock refreshes nothing instead
    of notifying twice.
    """
    statement = (
        insert(Notification)
        .values(
            job_id=job_id,
            user_id=user_id,
            job_type=job_type,
            channel=CHANNEL_IN_APP,
            title=title,
            body=body,
            claim_id=claim_id,
        )
        .on_conflict_do_nothing(index_elements=[Notification.job_id])
    )

    async with async_session_factory() as session:
        await session.execute(statement)
        await session.commit()


async def deliver_notification(  # noqa: PLR0913, PLR0917
    *,
    job_id: uuid.UUID,
    user_id: uuid.UUID,
    job_type: str,
    channel: str,
    payload: dict[str, Any],
    title: str,
    body: str,
) -> None:
    """Send one rendered notification through its channel's provider.

    This is the provider seam. ``in_app`` has a provider; ``email``, ``sms``, and
    ``webhook`` are reserved names that no provider is registered for, and they
    raise rather than succeeding. A silent success would mark the job completed and
    drop the notification on the floor, which is precisely the failure this outbox
    exists to prevent, so an unimplemented channel has to fail loudly, retry, and
    dead-letter where an operator can see it.

    Args:
        job_id: The outbox row delivering this notification.
        user_id: The recipient's UUID.
        job_type: Which delivery instruction produced this notification.
        channel: The delivery target named on the job.
        payload: The job's business payload, for a provider that needs it.
        title: The rendered subject line.
        body: The rendered message text.

    Raises:
        ValueError: No provider is registered for ``channel``.
    """
    if channel != CHANNEL_IN_APP:
        raise ValueError(
            f"No notification provider is registered for the '{channel}' channel. "
            "Register one in app.domain.notifications.outbox.deliver_notification; "
            f"'{channel}' is reserved."
        )

    claim_id = payload.get("claim_id")
    await _deliver_in_app(
        job_id=job_id,
        user_id=user_id,
        job_type=job_type,
        title=title,
        body=body,
        claim_id=str(claim_id) if claim_id else None,
    )


def _render_claim_submitted(payload: dict[str, Any]) -> tuple[str, str]:
    """Render the notification for a claim that has just been filed.

    Args:
        payload: The job payload, carrying ``claim_id`` and ``claim_type``.

    Returns:
        tuple[str, str]: The notification's title and body.
    """
    claim_id = payload.get("claim_id", "your claim")
    claim_type = str(payload.get("claim_type") or "claim").lower()
    return (
        f"Claim {claim_id} submitted",
        (
            f"We have received your {claim_type} claim {claim_id}. "
            "It is now recorded and waiting to be reviewed."
        ),
    )


def _render_claim_status_changed(payload: dict[str, Any]) -> tuple[str, str]:
    """Render the notification for a claim that moved to a new status.

    Args:
        payload: The job payload, carrying ``claim_id``, ``from_status``,
            ``to_status``, and any ``note`` the transition recorded.

    Returns:
        tuple[str, str]: The notification's title and body.
    """
    claim_id = payload.get("claim_id", "your claim")
    to_status = payload.get("to_status", "a new status")
    body = f"Claim {claim_id} is now {to_status}."
    from_status = payload.get("from_status")
    if from_status:
        body = f"Claim {claim_id} moved from {from_status} to {to_status}."
    note = payload.get("note")
    if note:
        body = f"{body} Note: {note}"
    return f"Claim {claim_id} is now {to_status}", body


def _render_claim_withdrawn(payload: dict[str, Any]) -> tuple[str, str]:
    """Render the notification for a claim the claimant withdrew.

    Args:
        payload: The job payload, carrying ``claim_id`` and any ``note``.

    Returns:
        tuple[str, str]: The notification's title and body.
    """
    claim_id = payload.get("claim_id", "your claim")
    body = f"You withdrew claim {claim_id}."
    note = payload.get("note")
    if note:
        body = f"{body} Reason: {note}"
    return f"Claim {claim_id} withdrawn", body


_TEMPLATES: dict[str, Any] = {
    JOB_TYPE_CLAIM_SUBMITTED: _render_claim_submitted,
    JOB_TYPE_CLAIM_STATUS_CHANGED: _render_claim_status_changed,
    JOB_TYPE_CLAIM_WITHDRAWN: _render_claim_withdrawn,
}


def render_notification(job_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Render a deterministic title and body for one notification job.

    A template table rather than a model call: a notification is a fixed string
    about a stored fact, the worker must not cost a model invocation per delivery,
    and the text a claimant reads must not change between two workers running two
    prompt versions.

    Args:
        job_type: The delivery instruction, one of ``NOTIFICATION_JOB_TYPES``.
        payload: The job's business data.

    Returns:
        tuple[str, str]: The rendered title and body.

    Raises:
        ValueError: ``job_type`` has no template.
    """
    template = _TEMPLATES.get(job_type)
    if template is None:
        raise ValueError(f"No notification template is registered for '{job_type}'.")
    return template(payload)


def enqueue_notification_job_payload(
    *,
    job_type: str,
    user_id: uuid.UUID,
    payload: dict[str, Any],
    channel: str = CHANNEL_IN_APP,
    max_attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Build the addressing envelope the store writes around a job's own payload.

    Kept separate from ``:func:enqueue_notification_job`` so a caller that wants
    to stage a job row itself — the way the claim submission flow stages its
    embedding job — still goes through one definition of the envelope.

    Args:
        job_type: The delivery instruction to enqueue.
        user_id: The recipient's UUID.
        payload: The business data the template renders from.
        channel: The delivery target. Defaults to the in-app feed.
        max_attempts: Attempt ceiling before the job is dead-lettered.

    Returns:
        dict[str, Any]: The store payload the outbox row is written from.
    """
    return {
        "job_type": job_type,
        "user_id": str(user_id),
        "channel": channel,
        "max_attempts": max_attempts,
        "data": dict(payload),
    }


async def enqueue_notification_job(  # noqa: PLR0913, PLR0917
    *,
    job_type: str,
    user_id: uuid.UUID,
    payload: dict[str, Any],
    channel: str = CHANNEL_IN_APP,
    max_attempts: int = MAX_ATTEMPTS,
    session: AsyncSession | None = None,
) -> None:
    """Queue a notification for asynchronous delivery.

    Delegates to ragnor's :func:`ragnor.jobs.enqueue_job`
    with the caller's session: when ``session`` is supplied,
    the job row is only added and flushed, so the claim change and
    the notification about it are persisted atomically by the
    caller's commit. When ``session`` is None, ragnor opens
    and commits its own transaction.

    A failure to enqueue is logged and never raised when this function owns the
    transaction: the claim change it describes is already durable, and failing a
    committed fact to protect a notification would trade one for the other.

    When ``session`` is supplied the situation is the opposite. The job row has
    only been *flushed*, not committed, so a flush failure has already poisoned
    the caller's transaction: its next ``commit()`` raises
    ``PendingRollbackError`` with a traceback that says nothing about the real
    cause. Re-raising here is what lets the caller fail on the actual error
    instead of a misleading one several statements later.

    Args:
        job_type: The delivery instruction, one of ``NOTIFICATION_JOB_TYPES``.
        user_id: The recipient's UUID.
        payload: The business data the template renders from.
        channel: The delivery target. Defaults to the in-app feed.
        max_attempts: Attempt ceiling before the job is dead-lettered.
        session: Optional existing database session. When provided, the job row
            joins the caller's transaction and the caller commits.
    """
    store_payload = enqueue_notification_job_payload(
        job_type=job_type,
        user_id=user_id,
        payload=payload,
        channel=channel,
        max_attempts=max_attempts,
    )
    store = NotificationJobStore()
    try:
        await enqueue_job(store, store_payload, session=session)
        logger.info("Enqueued '%s' notification for user '%s'.", job_type, user_id)
    except Exception as exc:
        if session is not None:
            raise
        logger.exception("Failed to enqueue '%s' notification: %s", job_type, exc)


async def process_pending_notification_jobs(
    limit: int = 10,
    worker_id: str | None = None,
) -> int:
    """Deliver pending notification jobs.

    Fetches up to ``limit`` pending jobs, renders and delivers each, and records
    the outcome. Failed deliveries are marked with an error detail and a retry
    timestamp; jobs that exhaust their attempts are dead-lettered.

    Claimed jobs are stamped with ``locked_at``/``locked_by`` for the whole
    processing transaction. A worker that dies mid-pass therefore leaves a
    visible lock, which :func:`reclaim_stale_notification_jobs` later resets.

    Args:
        limit: Maximum number of jobs to deliver in one batch.
        worker_id: Identity stamped into ``locked_by``. Defaults to
            ``settings.worker_id``.

    Returns:
        int: Number of jobs successfully delivered.
    """
    from app.core.config import settings  # noqa: PLC0415

    if worker_id is None:
        worker_id = settings.worker_id

    store = NotificationJobStore()
    processor = NotificationJobProcessor()
    return await _ragnor_process_pending_jobs(
        store,
        processor,
        limit=limit,
        worker_id=worker_id,
    )


async def reclaim_stale_notification_jobs(
    stale_after_seconds: float | None = None,
) -> int:
    """Reset notification jobs abandoned in ``processing`` back to ``pending``.

    A drainer that dies mid-delivery leaves its rows locked in ``processing``
    forever: the claim query only selects ``pending`` or retry-due ``failed`` rows,
    so no live worker can pick them up again. This reclaims rows whose
    ``locked_at`` is older than ``stale_after_seconds`` — or whose lock timestamp
    is NULL — so a replacement worker can deliver them.

    Args:
        stale_after_seconds: Lock age in seconds that counts as abandoned.
            Defaults to ``settings.job_stale_after_seconds``.

    Returns:
        int: Number of jobs reclaimed.
    """
    if stale_after_seconds is None:
        from app.core.config import settings  # noqa: PLC0415

        stale_after_seconds = settings.job_stale_after_seconds

    store = NotificationJobStore()
    return await _ragnor_reclaim_stale_jobs(
        store,
        stale_after_seconds=stale_after_seconds,
    )


__all__ = [
    "MAX_ATTEMPTS",
    "NOTIFICATION_JOB_TYPES",
    "NotificationJobProcessor",
    "NotificationJobStore",
    "deliver_notification",
    "enqueue_notification_job",
    "enqueue_notification_job_payload",
    "process_pending_notification_jobs",
    "reclaim_stale_notification_jobs",
    "render_notification",
]
