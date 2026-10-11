"""
Admin namespace: the operator's read surface over data no policyholder may see.

Four endpoints, every one behind ``require_role("admin")``:

  - ``GET /admin/claims``          -- the cross-owner claims queue.
  - ``GET /admin/embedding-jobs``  -- embedding-outbox depth and dead letters.
  - ``GET /admin/audit``           -- the paged audit trail.
  - ``GET /admin/feedback``        -- the feed of down-rated answers.

The routes answer the questions an operator previously could not ask without
shelling into the database: whose claims are stuck where, how deep the
embedding queue is and which jobs exhausted their retries, what every user did
to every entity, and which real answers users marked wrong. There is
deliberately no admin UI and no write surface here -- this is the read API a
follow-up console consumes, and nothing a browser can drive.

The response envelopes live in this module rather than in
``app.schemas.models`` because they describe rows owned by other modules: the
shared module is the frozen contract surface, and an admin page shape has no
reason to appear in it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_role
from app.database import get_db
from app.models.answer_feedback import AnswerFeedback
from app.models.audit_event import AuditEvent
from app.models.claim import Claim
from app.models.conversation_message import ConversationMessage
from app.models.embedding_job import EmbeddingJob
from app.models.user import User

# The router carries the ``/admin`` segment itself, so the include site -- the
# versioned router in ``router.py`` -- lands every route at /api/v1/admin/*
# without a second prefix to keep in step, and nothing here can ever be mounted
# at a top-level path by accident.
router = APIRouter(prefix="/admin")

# The terminal state a job reaches once it has exhausted its retries. It is the
# one status a human has to act on, so it is surfaced on every response.
_DEAD_LETTER_STATUS = "dead_letter"

_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


class AdminClaimItem(BaseModel):
    """One claim in the cross-owner queue.

    Attributes:
        claim_id: The human-readable claim identifier.
        policy_number: The policy the claim is filed against.
        claim_type: Claim category, e.g. "Water Damage".
        status: The claim's current lifecycle status.
        amount: Claimed amount in US dollars.
        owner_id: The policyholder who filed the claim.
        created_at: When the claim was filed.
    """

    claim_id: str = Field(..., description="Human-readable claim identifier")
    policy_number: str = Field(..., description="Policy the claim is filed against")
    claim_type: str = Field(..., description="Claim category")
    status: str = Field(..., description="Current lifecycle status")
    amount: Decimal = Field(..., description="Claimed amount in US dollars")
    owner_id: uuid.UUID = Field(..., description="Policyholder who filed the claim")
    created_at: datetime = Field(..., description="Filing timestamp")


class AdminClaimPage(BaseModel):
    """A page of the cross-owner claims queue.

    Unlike the policyholder's own claim list, this page is not scoped by owner:
    that is the entire point of the endpoint, and the reason it is admin-only.

    Attributes:
        claims: The claims on this page, newest first.
        count: How many claims are on this page.
        limit: The page size that produced this response.
        offset: How many newer claims were skipped.
    """

    claims: list[AdminClaimItem] = Field(..., description="Claims on this page, newest first")
    count: int = Field(..., description="Claims on this page")
    limit: int = Field(..., description="Page size used for this response")
    offset: int = Field(..., description="Newer claims skipped")


class EmbeddingJobItem(BaseModel):
    """One embedding-outbox job.

    Attributes:
        id: Primary key of the job.
        claim_id: The claim identifier the job embeds.
        owner_id: The claim's owner, so a stuck job can be traced to a user.
        status: Current job status, e.g. "failed" or "dead_letter".
        status_detail: Human-readable status or last error message.
        retry_count: How many times the job has been retried.
        max_attempts: Attempt budget before the job is dead-lettered.
        next_retry_at: When the job becomes eligible for retry, if it is waiting.
        locked_at: When a worker claimed the job, if it is processing.
        locked_by: The worker that claimed the job, if it is processing.
        created_at: When the job was enqueued.
        completed_at: When the job finished, if it has.
    """

    id: uuid.UUID = Field(..., description="Primary key of the job")
    claim_id: str = Field(..., description="Claim identifier the job embeds")
    owner_id: uuid.UUID = Field(..., description="Claim owner")
    status: str = Field(..., description="Current job status")
    status_detail: str | None = Field(default=None, description="Status or last error message")
    retry_count: int = Field(..., description="Retries attempted")
    max_attempts: int = Field(..., description="Attempt budget before dead-lettering")
    next_retry_at: datetime | None = Field(default=None, description="Retry eligibility")
    locked_at: datetime | None = Field(default=None, description="When a worker claimed the job")
    locked_by: str | None = Field(default=None, description="Worker that claimed the job")
    created_at: datetime = Field(..., description="Enqueue time")
    completed_at: datetime | None = Field(default=None, description="Completion time, if any")


class EmbeddingJobPage(BaseModel):
    """A page of the embedding outbox plus its queue-depth summary.

    Attributes:
        jobs: The jobs matching the status filter, newest first.
        counts: Job count per status across the whole queue, which is the
            queue depth. Computed unfiltered so an operator filtering to one
            status still sees where the rest of the queue stands.
        dead_letter: Every job that exhausted its retries, newest first. Always
            present, because a dead letter never recovers on its own.
        count: How many jobs are on this page.
        limit: The page size that produced this response.
        offset: How many newer jobs were skipped.
    """

    jobs: list[EmbeddingJobItem] = Field(..., description="Jobs matching the filter, newest first")
    counts: dict[str, int] = Field(..., description="Job count per status across the queue")
    dead_letter: list[EmbeddingJobItem] = Field(..., description="Jobs that exhausted retries")
    count: int = Field(..., description="Jobs on this page")
    limit: int = Field(..., description="Page size used for this response")
    offset: int = Field(..., description="Newer jobs skipped")


class AuditEventItem(BaseModel):
    """One entry in the audit trail.

    Attributes:
        id: Primary key of the entry.
        actor_id: Who performed the action, or None for a system or anonymous
            action such as a failed sign-in.
        action: The verb performed, e.g. "auth.signin".
        entity_type: The kind of thing acted on, e.g. "user" or "claim".
        entity_id: The identifier of the thing acted on.
        before: Fields as they were, or None.
        after: Fields as they are now, or None.
        ip_address: Client IP the action came from, when known.
        user_agent: Caller's user agent, when supplied.
        correlation_id: The request's ``X-Request-ID``, which ties the entry to
            its own line in the application log.
        created_at: When the entry was written.
    """

    id: uuid.UUID = Field(..., description="Primary key of the entry")
    actor_id: uuid.UUID | None = Field(default=None, description="Who performed the action")
    action: str = Field(..., description="Verb performed")
    entity_type: str = Field(..., description="Kind of thing acted on")
    entity_id: str | None = Field(default=None, description="Identifier of the thing acted on")
    before: dict | None = Field(default=None, description="Fields as they were")
    after: dict | None = Field(default=None, description="Fields as they are now")
    ip_address: str | None = Field(default=None, description="Client IP, when known")
    user_agent: str | None = Field(default=None, description="Caller user agent, when supplied")
    correlation_id: str | None = Field(default=None, description="Request correlation id")
    created_at: datetime = Field(..., description="Write time")


class AuditEventPage(BaseModel):
    """A page of the audit trail.

    Attributes:
        events: The entries on this page, newest first.
        count: How many entries are on this page.
        limit: The page size that produced this response.
        offset: How many newer entries were skipped.
    """

    events: list[AuditEventItem] = Field(..., description="Entries on this page, newest first")
    count: int = Field(..., description="Entries on this page")
    limit: int = Field(..., description="Page size used for this response")
    offset: int = Field(..., description="Newer entries skipped")


class DownRatedAnswerItem(BaseModel):
    """One answer a user rated unhelpful, with the text they rated.

    A down-rated real answer is the highest-value evaluation case a product can
    get, so the message text travels with the rating rather than being joined
    back by whoever reads the feed.

    Attributes:
        id: Primary key of the rating.
        message_id: The rated conversation message.
        user_id: The user who rated the answer.
        username: That user's username, so the feed can be triaged by account.
        category: The structured reason the user picked, if any.
        comment: The free-text reason the user typed, if any.
        created_at: When the rating was written.
        answer: The assistant answer that was rated.
    """

    id: uuid.UUID = Field(..., description="Primary key of the rating")
    message_id: uuid.UUID = Field(..., description="The rated conversation message")
    user_id: uuid.UUID = Field(..., description="The user who rated the answer")
    username: str = Field(..., description="The rating user's username")
    category: str | None = Field(default=None, description="Structured reason, when given")
    comment: str | None = Field(default=None, description="Free-text reason, when given")
    created_at: datetime = Field(..., description="When the rating was written")
    answer: str = Field(..., description="The assistant answer that was rated")


class DownRatedAnswerPage(BaseModel):
    """A page of the down-rated answers, newest first.

    Attributes:
        feedback: The ratings on this page.
        count: How many ratings are on this page.
        limit: The page size that produced this response.
        offset: How many newer ratings were skipped.
    """

    feedback: list[DownRatedAnswerItem] = Field(..., description="Ratings on this page")
    count: int = Field(..., description="Ratings on this page")
    limit: int = Field(..., description="Page size used for this response")
    offset: int = Field(..., description="Newer ratings skipped")


@router.get(
    "/claims",
    response_model=AdminClaimPage,
    summary="List Claims (Admin)",
    description=(
        "Returns every claim in the system, newest first, with optional status "
        "and owner filters. This is the cross-owner claims queue an operator "
        "needs; a policyholder's own list at /api/v1/claims is always scoped to "
        "that caller."
    ),
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "The caller is not an admin."},
    },
)
async def list_all_claims(
    status: str | None = Query(
        default=None,
        description="Filter to claims in this status (e.g. 'Under Review').",
    ),
    owner_id: uuid.UUID | None = Query(
        default=None,
        description="Filter to claims filed by this policyholder.",
    ),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    _admin: str = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> AdminClaimPage:
    """List claims across every owner.

    Args:
        status: Optional status filter.
        owner_id: Optional owner filter.
        limit: Page size.
        offset: Number of newest claims to skip.
        _admin: The authorization dependency, which resolves the caller's id
            once it has proven the caller is an admin.
        session: Async database session.

    Returns:
        AdminClaimPage: One page of the claims queue plus the pagination values
        that produced it.
    """
    stmt = select(Claim)
    if status is not None:
        stmt = stmt.where(Claim.status == status)
    if owner_id is not None:
        stmt = stmt.where(Claim.owner_id == owner_id)
    # claim_id breaks ties so a page ordered on a same-microsecond created_at is
    # still deterministic across repeated requests.
    stmt = stmt.order_by(Claim.created_at.desc(), Claim.claim_id.desc()).limit(limit).offset(offset)

    claims = (await session.execute(stmt)).scalars().all()

    return AdminClaimPage(
        claims=[
            AdminClaimItem(
                claim_id=claim.claim_id,
                policy_number=claim.policy_number,
                claim_type=claim.claim_type,
                status=claim.status,
                amount=claim.amount,
                owner_id=claim.owner_id,
                created_at=claim.created_at,
            )
            for claim in claims
        ],
        count=len(claims),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/embedding-jobs",
    response_model=EmbeddingJobPage,
    summary="List Embedding Jobs (Admin)",
    description=(
        "Returns the embedding outbox: a page of jobs (optionally filtered by "
        "status), the job count per status across the whole queue, and every "
        "job that exhausted its retries. The outbox exists because embedding "
        "latency must not sit on the claim submission path, which also means a "
        "stuck queue was invisible to humans until this endpoint existed."
    ),
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "The caller is not an admin."},
    },
)
async def list_embedding_jobs(
    status: str | None = Query(
        default=None,
        description="Filter to jobs in this status (e.g. 'failed', 'dead_letter').",
    ),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    _admin: str = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> EmbeddingJobPage:
    """List embedding outbox jobs with queue depth and dead letters.

    Args:
        status: Optional status filter for the paged job list.
        limit: Page size for both the job list and the dead-letter list.
        offset: Number of newest jobs to skip.
        _admin: The authorization dependency, which resolves the caller's id
            once it has proven the caller is an admin.
        session: Async database session.

    Returns:
        EmbeddingJobPage: One page of jobs, the per-status counts, and the
        dead-letter list.
    """
    stmt = select(EmbeddingJob)
    if status is not None:
        stmt = stmt.where(EmbeddingJob.status == status)
    # claim_id breaks ties so a page ordered on a same-microsecond created_at is
    # still deterministic across repeated requests.
    stmt = stmt.order_by(EmbeddingJob.created_at.desc(), EmbeddingJob.claim_id.desc())
    stmt = stmt.limit(limit).offset(offset)

    jobs = (await session.execute(stmt)).scalars().all()

    # Counted unfiltered: queue depth is the shape of the whole queue, and an
    # operator filtering to one status still needs to see where the rest stands.
    counts_rows = (
        await session.execute(
            select(EmbeddingJob.status, func.count()).group_by(EmbeddingJob.status)
        )
    ).all()
    counts = dict(counts_rows)

    dead_letter_rows = (
        (
            await session.execute(
                select(EmbeddingJob)
                .where(EmbeddingJob.status == _DEAD_LETTER_STATUS)
                .order_by(EmbeddingJob.created_at.desc(), EmbeddingJob.claim_id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    dead_letter = [_embedding_job_item(job) for job in dead_letter_rows]

    return EmbeddingJobPage(
        jobs=[_embedding_job_item(job) for job in jobs],
        counts=counts,
        dead_letter=dead_letter,
        count=len(jobs),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/audit",
    response_model=AuditEventPage,
    summary="List Audit Trail (Admin)",
    description=(
        "Returns the append-only audit trail, newest first, with optional "
        "entity, actor, and action filters. Not scoped by owner: the trail's "
        "value is that an operator can read what any account did to any entity."
    ),
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "The caller is not an admin."},
    },
)
async def list_audit_events(  # noqa: PLR0913 - one filter per column the trail is read by
    entity_type: str | None = Query(
        default=None,
        description="Filter to entries about this kind of thing (e.g. 'claim').",
    ),
    entity_id: str | None = Query(
        default=None,
        description="Filter to entries about this specific entity.",
    ),
    actor_id: uuid.UUID | None = Query(
        default=None,
        description="Filter to entries performed by this account.",
    ),
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    _admin: str = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> AuditEventPage:
    """List audit trail entries.

    Args:
        entity_type: Optional entity-kind filter.
        entity_id: Optional entity filter.
        actor_id: Optional actor filter.
        limit: Page size.
        offset: Number of newest entries to skip.
        _admin: The authorization dependency, which resolves the caller's id
            once it has proven the caller is an admin.
        session: Async database session.

    Returns:
        AuditEventPage: One page of the trail plus the pagination values that
        produced it.
    """
    stmt = select(AuditEvent)
    if entity_type is not None:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    # id breaks ties so a page written inside one timestamp is still ordered
    # deterministically across repeated requests.
    stmt = stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    stmt = stmt.limit(limit).offset(offset)

    events = (await session.execute(stmt)).scalars().all()

    return AuditEventPage(
        events=[
            AuditEventItem(
                id=entry.id,
                actor_id=entry.actor_id,
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                before=entry.before,
                after=entry.after,
                ip_address=entry.ip_address,
                user_agent=entry.user_agent,
                correlation_id=entry.correlation_id,
                created_at=entry.created_at,
            )
            for entry in events
        ],
        count=len(events),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/feedback",
    response_model=DownRatedAnswerPage,
    summary="List Down-Rated Answers (Admin)",
    description=(
        "Returns the answers users rated unhelpful, newest first, each with the "
        "answer text and the reason it was given. A real answer that users "
        "routinely find wrong is the highest-value evaluation input a product "
        "can get, and until this endpoint existed that signal was only visible "
        "by querying the table."
    ),
    responses={
        401: {"description": "Not authenticated."},
        403: {"description": "The caller is not an admin."},
    },
)
async def list_down_rated_answers(
    limit: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    _admin: str = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> DownRatedAnswerPage:
    """List the down-rated answers with their text.

    Args:
        limit: Page size.
        offset: Number of newer ratings to skip.
        _admin: The authorization dependency, which resolves the caller's id
            once it has proven the caller is an admin.
        session: Async database session.

    Returns:
        DownRatedAnswerPage: One page of down-rated answers plus the pagination
        values that produced it.
    """
    stmt = (
        select(AnswerFeedback, ConversationMessage.content, User.username)
        .join(ConversationMessage, ConversationMessage.id == AnswerFeedback.message_id)
        .join(User, User.id == AnswerFeedback.user_id)
        .where(AnswerFeedback.rating == "down")
        .order_by(AnswerFeedback.created_at.desc(), AnswerFeedback.id.desc())
        .limit(limit)
        .offset(offset)
    )

    rows = (await session.execute(stmt)).all()

    return DownRatedAnswerPage(
        feedback=[
            DownRatedAnswerItem(
                id=feedback.id,
                message_id=feedback.message_id,
                user_id=feedback.user_id,
                username=username,
                category=feedback.category,
                comment=feedback.comment,
                created_at=feedback.created_at,
                answer=content,
            )
            for feedback, content, username in rows
        ],
        count=len(rows),
        limit=limit,
        offset=offset,
    )


def _embedding_job_item(job: EmbeddingJob) -> EmbeddingJobItem:
    """Project a job row onto its response item.

    Args:
        job: The job ORM instance to project.

    Returns:
        EmbeddingJobItem: The job's fields as the response carries them.
    """
    return EmbeddingJobItem(
        id=job.id,
        claim_id=job.claim_id,
        owner_id=job.owner_id,
        status=job.status,
        status_detail=job.status_detail,
        retry_count=job.retry_count,
        max_attempts=job.max_attempts,
        next_retry_at=job.next_retry_at,
        locked_at=job.locked_at,
        locked_by=job.locked_by,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )
