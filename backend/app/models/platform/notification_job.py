"""
SQLAlchemy ORM models for the notification outbox and the in-app feed it fills.

Two tables, on purpose. ``notification_jobs`` is the outbox: a row per delivery
attempted in the claim transaction, carrying the retry bookkeeping a failing
consumer needs. ``notifications`` is the read model a claimant actually opens: one
row per *delivered* notification, denormalised into the text it renders, with a
read receipt. Keeping them apart means the outbox can gain channels, providers, or
attempt ceilings without the feed's shape following it, and it means a job that is
still pending, failing, or dead-lettered is simply absent from the feed rather
than appearing there as a husk.

The outbox deliberately reuses the ``embedding_jobs`` column set rather than
generalising that table: the drain worker, the claim guard, and the backoff policy
are proven there, and a claim-shaped NOT NULL schema is the wrong place to start a
notification queue.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

MAX_ATTEMPTS = 4
"""Attempt ceiling a notification job inherits unless the caller says otherwise.

Matches the outbox protocol's own default so a job that ignores the ceiling and a
job that accepts it dead-letter on the same attempt.
"""

# Delivery targets. ``in_app`` is the only channel with a provider; the rest are
# wired here so the enqueue call sites can name them today and a provider can be
# attached to one without a schema change.
CHANNEL_IN_APP = "in_app"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
CHANNEL_WEBHOOK = "webhook"

NOTIFICATION_CHANNELS: frozenset[str] = frozenset(
    {CHANNEL_IN_APP, CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_WEBHOOK}
)

# The claim facts worth telling a policyholder about unprompted. A ``job_type`` is
# a delivery instruction, not a status: the worker resolves each one to a rendered
# message from the payload, so no model call is needed to notify anybody.
JOB_TYPE_CLAIM_SUBMITTED = "claim.submitted"
JOB_TYPE_CLAIM_STATUS_CHANGED = "claim.status_changed"
JOB_TYPE_CLAIM_WITHDRAWN = "claim.withdrawn"

NOTIFICATION_JOB_TYPES: frozenset[str] = frozenset(
    {
        JOB_TYPE_CLAIM_SUBMITTED,
        JOB_TYPE_CLAIM_STATUS_CHANGED,
        JOB_TYPE_CLAIM_WITHDRAWN,
    }
)


class NotificationJob(Base):
    """Database model for one queued notification delivery.

    Attributes:
        id: Primary key UUID.
        user_id: The user the notification is for. The reference is enforced, so a
            deleted account takes its queued notifications with it rather than
            leaving rows that name a recipient nobody can look up.
        job_type: What happened, one of ``NOTIFICATION_JOB_TYPES``. Stored as text
            rather than an enum because a new delivery kind must not require a
            schema change, and the worker's template table is the authority on
            which values it can render.
        payload: The job's business data as JSONB, exactly what the template
            function reads.
        channel: Delivery target, one of ``NOTIFICATION_CHANNELS``. Defaults to the
            in-app feed, the only channel with a provider.
        status: Queue state (pending, processing, completed, failed, dead_letter).
        status_detail: Human-readable detail, including the last failure reason.
        retry_count: Number of failed attempts recorded so far.
        next_retry_at: When the job becomes eligible again after a failure.
        max_attempts: Attempt ceiling before the job is dead-lettered.
        locked_at: When a worker claimed the job, so a dead worker's lock can be
            reclaimed.
        locked_by: Identity of the worker that claimed the job.
        created_at: When the job was queued, in the same transaction as the claim
            change that caused it.
        completed_at: When delivery succeeded, or was given up on.
    """

    __tablename__ = "notification_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default=CHANNEL_IN_APP)
    # Not indexed on its own: the claim query filters on status and orders by
    # created_at together, which is the composite index below.
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    status_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=MAX_ATTEMPTS)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The claim query filters on status and orders by created_at, so both
        # columns belong in one index rather than one each.
        Index("ix_notification_jobs_status_created_at", status, created_at),
    )


class Notification(Base):
    """Database model for one delivered notification, as the recipient reads it.

    The feed is written by the drain worker, never by the request that caused the
    notification: a claimant must not wait on a delivery to move a claim, and the
    claim transaction must not be able to fail because a feed row would not insert.

    Attributes:
        id: Primary key UUID.
        user_id: The user whose feed holds the notification.
        job_id: The outbox row that delivered it. Unique, so a redelivery after a
            reclaimed lock cannot put the same notification in a feed twice.
        job_type: Which delivery instruction produced this, for the client to key
            on.
        channel: Which provider delivered it, so a future email notification and
            the in-app copy of the same event stay distinguishable.
        title: Rendered subject line, stored rather than recomputed so a template
            change never rewrites what a user has already read.
        body: Rendered message text.
        claim_id: The claim the notification is about, when it has one. Plain text
            rather than a reference, because the feed must stay readable for a
            claim that has since been deleted.
        read_at: When the user acknowledged it, or NULL while it is still unread.
        created_at: When the notification was delivered, generated by the database
            so rows written by a worker with a skewed clock still order correctly.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default=CHANNEL_IN_APP)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)
    claim_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # One delivered row per outbox job: a worker that lost its lock mid-pass
        # retries the job, and without this the retry would notify a second time.
        UniqueConstraint("job_id", name="uq_notifications_job_id"),
        # The feed read is "this user's notifications, newest first, unread
        # filtered", so the owner and the ordering columns share one index.
        Index("ix_notifications_user_id_created_at", user_id, created_at),
    )


__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_IN_APP",
    "CHANNEL_SMS",
    "CHANNEL_WEBHOOK",
    "JOB_TYPE_CLAIM_STATUS_CHANGED",
    "JOB_TYPE_CLAIM_SUBMITTED",
    "JOB_TYPE_CLAIM_WITHDRAWN",
    "MAX_ATTEMPTS",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_JOB_TYPES",
    "Notification",
    "NotificationJob",
]
