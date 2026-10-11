"""
Claims API endpoints for the OmniCare backend.

Provides claim status lookup and the two-step claim submission flow
(prepare -> confirm) to prevent unauthorized claim submissions, plus the
owner-scoped read surface a policyholder uses to view their own claims, and
the lifecycle endpoints that move a claim through its statuses while recording
the transition history.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.submit_claim import submit_claim_internal
from app.auth import get_current_user
from app.database import async_session_factory, get_db
from app.domain.claims.outbox import enqueue_embedding_job
from app.domain.notifications.outbox import enqueue_notification_job
from app.models.claim import Claim
from app.models.claim_event import (
    POLICYHOLDER_STATUSES,
    ClaimEvent,
    ClaimStatus,
    allowed_next_statuses,
    is_status_transition_allowed,
    parse_claim_status,
)
from app.models.claim_submission import ClaimSubmission
from app.models.notification_job import (
    JOB_TYPE_CLAIM_STATUS_CHANGED,
    JOB_TYPE_CLAIM_SUBMITTED,
    JOB_TYPE_CLAIM_WITHDRAWN,
)
from app.schemas.models import (
    ClaimCancelRequest,
    ClaimCancelResponse,
    ClaimConfirmationRequest,
    ClaimConfirmationResponse,
    ClaimDetail,
    ClaimEventItem,
    ClaimListResponse,
    ClaimStatusUpdateRequest,
    ClaimSubmissionPrepareRequest,
    ClaimSubmissionPrepareResponse,
    ClaimSummary,
    ClaimTransitionResponse,
    ClaimWithdrawRequest,
    ErrorResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Interim authorization seam for administrative status transitions. The only
# caller identity an authenticated request carries is that user's UUID, so until
# a role model exists the endpoint cannot prove the caller is a claims handler.
# Administrative transitions are therefore refused to everyone, and the tests
# that drive the administrative half of the lifecycle widen this set for their
# own actor. Swapping this constant for a role lookup is the whole of the change
# the authorization work needs to make here.
ADMINISTRATIVE_TRANSITION_ACTORS: frozenset[str] = frozenset()


@router.post(
    "/prepare",
    response_model=ClaimSubmissionPrepareResponse,
    status_code=status.HTTP_200_OK,
    summary="Prepare Claim Submission",
    description=(
        "Validates claim data and creates a pending submission record. "
        "Returns a confirmation_token that the frontend must present to the "
        "/confirm endpoint to complete the submission."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Pending submission created successfully.",
            "model": ClaimSubmissionPrepareResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Invalid claim data.",
            "model": ErrorResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Invalid claim data or malformed incident date.",
            "model": ErrorResponse,
        },
    },
)
async def prepare_claim_submission(
    payload: ClaimSubmissionPrepareRequest,
    incident_date: date | None = Query(
        default=None,
        description=(
            "Date the incident occurred. Optional; a claim filed without one stores "
            "a null incident date rather than guessing from the filing date."
        ),
    ),
    current_user_id: str = Depends(get_current_user),
) -> ClaimSubmissionPrepareResponse:
    """Validate claim data and create a pending submission awaiting confirmation.

    This is the first step of the two-step claim submission flow. The agent
    collects the required fields, calls this endpoint to validate them, and
    receives a confirmation_token. The frontend displays a confirmation UI
    and calls /confirm with the token to complete the submission.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    # Validate via Pydantic (already done by FastAPI) and prepare claim data
    claim_data = payload.model_dump(mode="json")
    # Serialize Decimal to string for JSONB storage to preserve precision
    # and avoid float rounding issues.
    claim_data["amount"] = str(claim_data["amount"])
    # The pending submission is the only place the incident date can travel between
    # prepare and confirm, because the confirm request carries just a token.
    if incident_date is not None:
        claim_data["incident_date"] = incident_date.isoformat()

    confirmation_token = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    try:
        async with async_session_factory() as session:
            submission = ClaimSubmission(
                user_id=user_uuid,
                confirmation_token=confirmation_token,
                claim_data=claim_data,
                status="pending",
                expires_at=expires_at,
            )
            session.add(submission)
            await session.commit()
    except SQLAlchemyError as exc:
        logger.exception("Failed to create pending claim submission: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to prepare claim submission. Please try again.",
        ) from exc

    logger.info(
        "Prepared claim submission for user '%s' with token '%s'.",
        current_user_id,
        confirmation_token,
    )

    return ClaimSubmissionPrepareResponse(
        confirmation_token=confirmation_token,
        expires_at=expires_at,
        status="pending",
    )


@router.post(
    "/confirm",
    response_model=ClaimConfirmationResponse,
    status_code=status.HTTP_200_OK,
    summary="Confirm Claim Submission",
    description=(
        "Confirms a pending claim submission using the confirmation_token "
        "from the /prepare endpoint. The backend validates the token belongs "
        "to the authenticated user and is not expired before submitting."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "Claim confirmed and submitted successfully.",
            "model": ClaimConfirmationResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Invalid or expired confirmation token.",
            "model": ErrorResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
    },
)
async def confirm_claim_submission(
    payload: ClaimConfirmationRequest,
    current_user_id: str = Depends(get_current_user),
) -> ClaimConfirmationResponse:
    """Confirm and submit a pending claim submission.

    Validates the confirmation token belongs to the authenticated user,
    checks it is not expired, and then calls the internal submit logic.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    async with async_session_factory() as session:
        submission = (
            await session.execute(
                select(ClaimSubmission)
                .where(
                    ClaimSubmission.confirmation_token == payload.confirmation_token,
                    ClaimSubmission.user_id == user_uuid,
                    ClaimSubmission.status == "pending",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if submission is None:
            logger.warning(
                "Invalid or expired confirmation token '%s' for user '%s'.",
                payload.confirmation_token,
                current_user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid, expired, or already used confirmation token.",
            )

        if submission.expires_at < datetime.now(UTC):
            submission.status = "expired"
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Confirmation token has expired.",
            )

        claim_data = submission.claim_data
        result = await submit_claim_internal(
            policy_number=claim_data["policy_number"],
            claim_type=claim_data["claim_type"],
            amount=float(claim_data["amount"]),
            description=claim_data["description"],
            user_uuid=user_uuid,
            session=session,
        )

        if not result.get("success"):
            logger.error(
                "Claim submission failed for token '%s': %s",
                payload.confirmation_token,
                result.get("error", "Unknown error"),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result.get("error", "Claim submission failed."),
            )

        await _record_incident_date(
            session,
            owner_uuid=user_uuid,
            claim_id=result.get("confirmation_id"),
            incident_date=_parse_incident_date(submission.claim_data.get("incident_date")),
        )
        await _record_filing_event(
            session,
            owner_uuid=user_uuid,
            claim_id=result.get("confirmation_id"),
        )
        # Same transaction as the claim, the filing event, and the embedding job:
        # the notification describes a claim that only exists once this commit
        # lands. The session is passed so the job row cannot outlive a rollback and
        # tell a policyholder their claim was filed when it was not.
        await enqueue_notification_job(
            job_type=JOB_TYPE_CLAIM_SUBMITTED,
            user_id=user_uuid,
            payload={
                "claim_id": result.get("confirmation_id"),
                "claim_type": result.get("claim_type"),
                "status": result.get("status"),
            },
            session=session,
        )

        submission.status = "completed"
        await session.commit()

    logger.info(
        "Claim confirmed for user '%s': %s",
        current_user_id,
        result.get("message", ""),
    )

    return ClaimConfirmationResponse(
        success=result.get("success", False),
        claim_id=result.get("confirmation_id"),
        status=result.get("status"),
        message=result.get("message", "Claim submission processed."),
    )


@router.post(
    "/cancel",
    response_model=ClaimCancelResponse,
    status_code=status.HTTP_200_OK,
    summary="Cancel Pending Claim Submission",
    description=(
        "Abandons a claim submission the caller prepared but never confirmed. "
        "The pending submission is marked cancelled so its confirmation token "
        "can never be redeemed. The claim itself is never created, because a "
        "cancelled submission never reaches the confirm flow."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "The pending submission was cancelled.",
            "model": ClaimCancelResponse,
        },
        status.HTTP_400_BAD_REQUEST: {
            "description": "Invalid, expired, or already used confirmation token.",
            "model": ErrorResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
    },
)
async def cancel_claim_submission(
    payload: ClaimCancelRequest,
    current_user_id: str = Depends(get_current_user),
) -> ClaimCancelResponse:
    """Cancel a pending claim submission the caller has not confirmed yet.

    The submission row is looked up under the same ``FOR UPDATE`` lock the
    confirm flow takes, so a cancel racing a confirm cannot leave the row half
    written: one of the two holds the lock and the other sees the row's new
    status.

    Args:
        payload: The confirmation token identifying the pending submission.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.

    Returns:
        ClaimCancelResponse: Success plus a human-readable message.

    Raises:
        HTTPException: 400 if the token is unknown to this user, expired, or
            already used; 401 if the authenticated user id is not a valid UUID.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    async with async_session_factory() as session:
        submission = (
            await session.execute(
                select(ClaimSubmission)
                .where(
                    ClaimSubmission.confirmation_token == payload.confirmation_token,
                    ClaimSubmission.user_id == user_uuid,
                    ClaimSubmission.status == "pending",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        # A token belonging to another user is indistinguishable from one that
        # was never issued, so both report the same refusal.
        if submission is None:
            logger.warning(
                "Cancel requested with an unusable confirmation token '%s' for user '%s'.",
                payload.confirmation_token,
                current_user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid, expired, or already used confirmation token.",
            )

        if submission.expires_at < datetime.now(UTC):
            submission.status = "expired"
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Confirmation token has expired.",
            )

        submission.status = "cancelled"
        await session.commit()

    logger.info(
        "Cancelled pending claim submission for user '%s'.",
        current_user_id,
    )

    return ClaimCancelResponse(
        success=True,
        message="Claim submission cancelled. No claim was filed.",
    )


@router.get(
    "",
    response_model=ClaimListResponse,
    summary="List Claims",
    description=(
        "Returns the authenticated policyholder's own claims, newest first. "
        "Every row is filtered on the caller's owner id, so another user's claim "
        "can never appear in the response."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "A page of the caller's claims.",
            "model": ClaimListResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
    },
)
async def list_claims(  # noqa: PLR0913
    claim_status: str | None = Query(
        default=None,
        alias="status",
        description="Filter to claims in this status (e.g. 'Submitted', 'Under Review').",
    ),
    claim_type: str | None = Query(
        default=None,
        description="Filter to claims of this type (e.g. 'Water Damage').",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of claims to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of newer claims to skip.",
    ),
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ClaimListResponse:
    """List the authenticated user's claims, newest first.

    The query is scoped to ``owner_id == current_user_id`` before any filter or
    pagination is applied, so a caller who guesses another user's claim id still
    gets an empty page rather than that claim's row.

    Args:
        claim_status: Optional status filter.
        claim_type: Optional claim-type filter.
        limit: Page size, defaulting to 20 and capped at 50.
        offset: Number of newest claims to skip.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        ClaimListResponse: One page of the caller's claims plus the pagination
        values that produced it.

    Raises:
        HTTPException: 401 if the authenticated user id is not a valid UUID.
    """
    try:
        owner_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    stmt = select(Claim).where(Claim.owner_id == owner_uuid)
    if claim_status is not None:
        stmt = stmt.where(Claim.status == claim_status)
    if claim_type is not None:
        stmt = stmt.where(Claim.claim_type == claim_type)
    # claim_id breaks ties so a page ordered on a same-microsecond created_at is
    # still deterministic across repeated requests.
    stmt = stmt.order_by(desc(Claim.created_at), desc(Claim.claim_id))
    stmt = stmt.limit(limit).offset(offset)

    claims = (await session.execute(stmt)).scalars().all()

    return ClaimListResponse(
        claims=[ClaimSummary(**_claim_fields(claim)) for claim in claims],
        count=len(claims),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{claim_id}",
    response_model=ClaimDetail,
    summary="Get Claim",
    description=(
        "Returns one of the authenticated policyholder's claims with its "
        "description. Another user's claim id returns 404 rather than 403 so the "
        "response cannot confirm the claim exists."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "The requested claim.",
            "model": ClaimDetail,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No claim with that id belongs to the caller.",
            "model": ErrorResponse,
        },
    },
)
async def get_claim(
    claim_id: str,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ClaimDetail:
    """Retrieve a single claim owned by the authenticated user.

    Args:
        claim_id: The human-readable claim identifier, case-insensitive.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        ClaimDetail: The claim, its description, a citation string, and its
        lifecycle events, newest first.

    Raises:
        HTTPException: 401 if the authenticated user id is not a valid UUID,
            404 if the claim does not exist or belongs to another user.
    """
    try:
        owner_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    # SECURITY: Both predicates live in the WHERE clause so a claim id belonging to
    # another user is indistinguishable from one that was never issued.
    claim = (
        await session.execute(
            select(Claim).where(
                Claim.claim_id == claim_id.strip().upper(),
                Claim.owner_id == owner_uuid,
            )
        )
    ).scalar_one_or_none()

    if claim is None:
        logger.warning(
            "Claim '%s' requested by non-owner '%s'; reported as not found.",
            claim_id,
            current_user_id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

    events = await _claim_events(session, claim)

    return ClaimDetail(
        **_claim_fields(claim),
        description=claim.description,
        citation=(
            f"Claim {claim.claim_id} in the OmniCare claims system "
            f"(policy {claim.policy_number})."
        ),
        events=events,
    )


@router.post(
    "/{claim_id}/status",
    response_model=ClaimTransitionResponse,
    summary="Change Claim Status",
    description=(
        "Moves one of the authenticated policyholder's claims to a new status. "
        "The transition must be one the claim lifecycle allows; otherwise the "
        "response names the statuses the claim may still move to. A policyholder "
        "may only withdraw their own claim; every other transition is a handler "
        "decision and is refused until an authorization model can prove the "
        "caller is one. Changing the status re-enqueues the claim for embedding "
        "so search results stop reporting the status it had when it was filed."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "The claim moved to the requested status.",
            "model": ClaimTransitionResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "The caller may not perform this transition.",
            "model": ErrorResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No claim with that id belongs to the caller.",
            "model": ErrorResponse,
        },
        status.HTTP_409_CONFLICT: {
            "description": "The transition is not allowed from the current status.",
            "model": ErrorResponse,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "The requested status is not part of the claim lifecycle.",
            "model": ErrorResponse,
        },
    },
)
async def update_claim_status(
    claim_id: str,
    payload: ClaimStatusUpdateRequest,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ClaimTransitionResponse:
    """Move one of the caller's claims to a new lifecycle status.

    Args:
        claim_id: The human-readable claim identifier, case-insensitive.
        payload: The requested status and an optional note for the event record.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        ClaimTransitionResponse: The status the claim moved from and to, plus
        the statuses it may still move to.

    Raises:
        HTTPException: 401 for an invalid user id, 404 when the claim is not the
            caller's, 422 for a status outside the lifecycle, 409 for a
            transition the lifecycle forbids, 403 for an administrative
            transition requested by a non-administrative caller.
    """
    try:
        owner_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    target = parse_claim_status(payload.status)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"'{payload.status}' is not a claim status. "
                f"Valid statuses: {', '.join(status_.value for status_ in ClaimStatus)}."
            ),
        )

    return await _transition_claim(
        session,
        owner_uuid=owner_uuid,
        actor_uuid=owner_uuid,
        claim_id=claim_id,
        target=target,
        note=payload.note,
        actor_summary="the policyholder",
    )


@router.post(
    "/{claim_id}/withdraw",
    response_model=ClaimTransitionResponse,
    summary="Withdraw Claim",
    description=(
        "Withdraws one of the authenticated policyholder's own claims. This is "
        "the one lifecycle transition a policyholder may perform themselves: a "
        "claim that is already under review can no longer be withdrawn once a "
        "handler has decided it."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "The claim was withdrawn.",
            "model": ClaimTransitionResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No claim with that id belongs to the caller.",
            "model": ErrorResponse,
        },
        status.HTTP_409_CONFLICT: {
            "description": "The claim cannot be withdrawn from its current status.",
            "model": ErrorResponse,
        },
    },
)
async def withdraw_claim(
    claim_id: str,
    payload: ClaimWithdrawRequest,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ClaimTransitionResponse:
    """Withdraw one of the caller's own claims.

    Args:
        claim_id: The human-readable claim identifier, case-insensitive.
        payload: An optional reason, recorded on the lifecycle event.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        ClaimTransitionResponse: The status the claim moved from and to, plus
        the statuses it may still move to.

    Raises:
        HTTPException: 401 for an invalid user id, 404 when the claim is not the
            caller's, 409 when the lifecycle does not allow a withdrawal from the
            claim's current status.
    """
    try:
        owner_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    return await _transition_claim(
        session,
        owner_uuid=owner_uuid,
        actor_uuid=owner_uuid,
        claim_id=claim_id,
        target=ClaimStatus.WITHDRAWN,
        note=payload.reason,
        actor_summary="the policyholder",
    )


def _claim_fields(claim: Claim) -> dict[str, Any]:
    """Project a claim row onto the fields both read responses share.

    Args:
        claim: The claim ORM instance to project.

    Returns:
        dict[str, Any]: The claim's identifiers, status, amount, incident date,
        and filing timestamp.
    """
    return {
        "claim_id": claim.claim_id,
        "policy_number": claim.policy_number,
        "claim_type": claim.claim_type,
        "status": claim.status,
        "amount": claim.amount,
        "incident_date": claim.incident_date,
        "created_at": claim.created_at,
    }


async def _claim_events(session: AsyncSession, claim: Claim) -> list[dict[str, Any]]:
    """Read one claim's lifecycle history, newest transition first.

    The query is scoped by the claim row the caller already owns, so a history
    can never be read for a claim the caller cannot see.

    Args:
        session: The session reading the history.
        claim: The claim whose events are read.

    Returns:
        list[dict[str, Any]]: One serialized event per transition, ordered so
        the most recent transition comes first. Empty for a claim that has no
        recorded history.
    """
    events = (
        (
            await session.execute(
                select(ClaimEvent)
                .where(ClaimEvent.claim_uuid == claim.id)
                .order_by(desc(ClaimEvent.created_at), desc(ClaimEvent.id))
            )
        )
        .scalars()
        .all()
    )
    return [
        ClaimEventItem.model_validate(event, from_attributes=True).model_dump(mode="json")
        for event in events
    ]


async def _transition_claim(  # noqa: PLR0913
    session: AsyncSession,
    *,
    owner_uuid: uuid.UUID,
    actor_uuid: uuid.UUID,
    claim_id: str,
    target: ClaimStatus,
    note: str | None,
    actor_summary: str,
) -> ClaimTransitionResponse:
    """Validate and apply one lifecycle transition in the caller's transaction.

    Args:
        session: The transaction the claim row, the event row, the embedding job,
            and the notification job are written in. The caller commits, so a
            rejected transition writes nothing at all.
        owner_uuid: The user who must own the claim for the write to proceed.
        actor_uuid: Who is performing the transition, recorded on the event.
        claim_id: The human-readable claim identifier.
        target: The status to move the claim to.
        note: Optional reason to record on the transition.
        actor_summary: How the actor is described in log messages.

    Returns:
        ClaimTransitionResponse: The status the claim moved from and to, plus
        the statuses it may still move to.

    Raises:
        HTTPException: 404 when the claim is not the caller's, 409 when the
            lifecycle forbids the transition, 403 when the transition is
            administrative and the caller has no authority for it.
    """
    claim = (
        await session.execute(
            select(Claim)
            .where(Claim.claim_id == claim_id.strip().upper(), Claim.owner_id == owner_uuid)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if claim is None:
        logger.warning(
            "Claim '%s' status change requested by non-owner '%s'; reported as not found.",
            claim_id,
            owner_uuid,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")

    previous_status = claim.status
    current = parse_claim_status(previous_status)
    if not is_status_transition_allowed(current, target):
        allowed = allowed_next_statuses(current)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_illegal_transition_detail(claim.claim_id, previous_status, target, allowed),
        )

    await _require_transition_authority(actor_uuid, target)

    claim.status = target.value
    session.add(
        ClaimEvent(
            claim_uuid=claim.id,
            actor_id=actor_uuid,
            from_status=current.value if current is not None else previous_status,
            to_status=target.value,
            note=note,
        )
    )
    await session.flush()

    # The claim's indexed metadata carries the status and amount as they were at
    # embed time. Without this enqueue the vector store would answer with the
    # status the claim had when it was filed, however many times it had moved on.
    # The session is passed, so the job row commits with the status it describes
    # instead of racing it.
    await enqueue_embedding_job(
        claim_uuid=claim.id,
        claim_id=claim.claim_id,
        owner_id=claim.owner_id,
        claim_type=claim.claim_type,
        description=claim.description,
        policy_number=claim.policy_number,
        claim_status=claim.status,
        session=session,
    )
    await _enqueue_transition_notification(
        session,
        claim=claim,
        owner_uuid=owner_uuid,
        previous_status=previous_status,
        target=target,
        note=note,
    )
    await session.commit()

    logger.info(
        "Claim '%s' moved from '%s' to '%s' by %s.",
        claim.claim_id,
        previous_status,
        target.value,
        actor_summary,
    )

    allowed = allowed_next_statuses(target)
    message = f"Claim {claim.claim_id} is now {target.value}."
    if allowed:
        message = f"{message} It can move to: {', '.join(allowed)}."
    return ClaimTransitionResponse(
        claim_id=claim.claim_id,
        previous_status=previous_status,
        status=target.value,
        note=note,
        allowed_next_statuses=list(allowed),
        message=message,
    )


async def _require_transition_authority(
    actor_uuid: uuid.UUID,
    target: ClaimStatus,
) -> None:
    """Refuse an administrative transition the caller has no authority for.

    The authority check is the one place this route needs a role, and no role
    exists yet. Until one does, a caller who is not recorded in
    ``ADMINISTRATIVE_TRANSITION_ACTORS`` cannot move anyone's claim through a
    handler decision at all.

    Args:
        actor_uuid: The user performing the transition.
        target: The status being requested.

    Raises:
        HTTPException: 403 when the target status is administrative and the
            caller is not one of the interim administrative actors.
    """
    if target in POLICYHOLDER_STATUSES:
        return
    if str(actor_uuid) in ADMINISTRATIVE_TRANSITION_ACTORS:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"Moving a claim to '{target.value}' requires a claims handler. "
            "A policyholder may only withdraw their own claim."
        ),
    )


def _illegal_transition_detail(
    claim_id: str,
    current_status: str,
    target: ClaimStatus,
    allowed: tuple[str, ...],
) -> str:
    """Build the 409 message that names the statuses a claim may still move to.

    Args:
        claim_id: The human-readable claim identifier.
        current_status: The status the claim currently holds.
        target: The status that was requested.
        allowed: The statuses the lifecycle permits from the current one.

    Returns:
        str: A message naming the refused transition and the legal next states.
    """
    prefix = f"Cannot move claim '{claim_id}' from '{current_status}' to '{target.value}'. "
    if allowed:
        return f"{prefix}Allowed next statuses: {', '.join(allowed)}."
    return f"{prefix}'{current_status}' has no allowed next status."


async def _enqueue_transition_notification(  # noqa: PLR0913
    session: AsyncSession,
    *,
    claim: Claim,
    owner_uuid: uuid.UUID,
    previous_status: str,
    target: ClaimStatus,
    note: str | None,
) -> None:
    """Queue the notification that tells the owner their claim moved.

    Runs inside the caller's transaction and never commits: the status change, its
    event, the re-index job, and this notification become durable together or not
    at all. Without the shared transaction a notification could describe a
    transition that was rolled back, and a claimant has no way to un-read it.

    A withdrawal is its own delivery kind rather than a status change, because the
    claimant performed it themselves: the rendered message must say so instead of
    reporting a move as something that happened to their claim.

    Args:
        session: The transaction the transition is written in.
        claim: The claim that moved, already updated in this session.
        owner_uuid: The user the notification belongs to.
        previous_status: The status the claim held before the transition.
        target: The status the claim moved to.
        note: Optional reason the caller recorded on the transition.
    """
    job_type = (
        JOB_TYPE_CLAIM_WITHDRAWN
        if target is ClaimStatus.WITHDRAWN
        else JOB_TYPE_CLAIM_STATUS_CHANGED
    )
    await enqueue_notification_job(
        job_type=job_type,
        user_id=owner_uuid,
        payload={
            "claim_id": claim.claim_id,
            "claim_uuid": str(claim.id),
            "user_id": str(owner_uuid),
            "from_status": previous_status,
            "to_status": target.value,
            "note": note,
        },
        session=session,
    )


def _parse_incident_date(raw: object) -> date | None:
    """Interpret the incident date captured at prepare time.

    Args:
        raw: The stored value, expected to be an ISO-8601 date string.

    Returns:
        date | None: The parsed date, or None when no date was captured or the
        stored value is unusable. A malformed value is discarded rather than
        failing a claim the policyholder already confirmed.
    """
    if raw is None:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        logger.warning("Discarding malformed incident date on a pending claim submission.")
        return None


async def _record_incident_date(
    session: AsyncSession,
    owner_uuid: uuid.UUID,
    claim_id: str | None,
    incident_date: date | None,
) -> None:
    """Write the incident date onto the claim created by this confirmation.

    Runs inside the caller's transaction and never commits: the surrounding
    confirm flow already commits the claim, the embedding job, and the
    submission status change together.

    Args:
        session: The transaction the claim was written in.
        owner_uuid: The confirming user, so only that user's claim is touched.
        claim_id: The claim id returned by the submission, or None when it is
            absent.
        incident_date: The date to store, or None to skip the update entirely.
    """
    if incident_date is None or not claim_id:
        return

    claim = (
        await session.execute(
            select(Claim).where(Claim.claim_id == claim_id, Claim.owner_id == owner_uuid)
        )
    ).scalar_one_or_none()

    if claim is None:
        logger.warning("Could not attach incident date: claim '%s' was not found.", claim_id)
        return

    claim.incident_date = incident_date


async def _record_filing_event(
    session: AsyncSession,
    owner_uuid: uuid.UUID,
    claim_id: str | None,
) -> None:
    """Write the opening entry of the lifecycle history for a filed claim.

    Runs inside the caller's transaction and never commits, so the claim, its
    embedding job, and the first entry in its history become durable together.
    Without this entry a claim's history would start empty and stay empty until
    somebody moved it, which would leave "how long has this been pending?"
    unanswerable for a claim that has only ever been pending.

    Args:
        session: The transaction the claim was written in.
        owner_uuid: The confirming user, so only that user's claim is touched.
        claim_id: The claim id returned by the submission, or None when it is
            absent.
    """
    if not claim_id:
        return

    claim = (
        await session.execute(
            select(Claim).where(Claim.claim_id == claim_id, Claim.owner_id == owner_uuid)
        )
    ).scalar_one_or_none()

    if claim is None:
        logger.warning("Could not record the filing event: claim '%s' was not found.", claim_id)
        return

    session.add(
        ClaimEvent(
            claim_uuid=claim.id,
            actor_id=owner_uuid,
            from_status=None,
            to_status=ClaimStatus.SUBMITTED.value,
            note="Claim filed.",
        )
    )
    await session.flush()
