"""
Claim submission tool and internal function for the OmniCare agent.

The agent-facing tool `prepare_claim_submission` validates inputs with Pydantic
and creates a pending record in the claim_submissions table, returning a
confirmation_token. The internal function `submit_claim_internal` performs the
actual database write after the user has confirmed via the frontend.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import current_user_id
from app.core.database import async_session_factory
from app.models.claims.claim import Claim
from app.models.claims.claim_event import ClaimStatus
from app.models.claims.claim_submission import ClaimSubmission as ClaimSubmissionModel
from app.models.claims.embedding_job import EmbeddingJob
from app.schemas.models import ClaimSubmission

logger = logging.getLogger(__name__)


async def prepare_claim_submission(
    policy_number: str,
    claim_type: str,
    amount: float,
    description: str,
) -> dict[str, Any]:
    """Prepare a new OmniCare insurance claim submission for confirmation.

    Use this tool when the user has confirmed they want to file a new claim
    after all required fields have been collected. This tool validates the
    inputs and creates a pending record in the database, returning a
    confirmation_token that the frontend must present to the /confirm endpoint.

    The agent should instruct the user to confirm the submission in the UI
    after receiving the confirmation_token.

    Args:
        policy_number: The policyholder's policy number (e.g., "POL-1092").
        claim_type: Category of the claim (e.g., "Water Damage", "Personal Property").
        amount: Claimed amount in US dollars. Must be greater than 0.
        description: Factual description of the incident (minimum 10 characters).

    Returns:
        dict: Preparation result. On success, includes success=True,
        confirmation_token, expires_at, and a message instructing the user
        to confirm. On failure, includes success=False and an error message.
    """
    try:
        user_uuid = current_user_id.get()
    except LookupError:
        logger.error("current_user_id not found in context during claim preparation.")
        return {"success": False, "error": "Unauthorized submission."}

    try:
        validated = ClaimSubmission(
            policy_number=policy_number,
            claim_type=claim_type,
            amount=amount,
            description=description,
        )
    except ValidationError as exc:
        errors = [
            f"{err['loc'][-1] if err['loc'] else 'field'}: {err['msg']}" for err in exc.errors()
        ]
        logger.warning("Claim preparation validation failed: %s", errors)
        return {"success": False, "validation_errors": errors}

    confirmation_token = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(minutes=15)

    claim_data = {
        "policy_number": validated.policy_number,
        "claim_type": validated.claim_type,
        "amount": str(validated.amount),
        "description": validated.description,
    }

    try:
        async with async_session_factory() as session:
            submission = ClaimSubmissionModel(
                user_id=user_uuid,
                confirmation_token=confirmation_token,
                claim_data=claim_data,
                status="pending",
                expires_at=expires_at,
            )
            session.add(submission)
            await session.commit()
    except Exception as exc:
        logger.exception("Failed to persist pending claim submission: %s", exc)
        return {
            "success": False,
            "error": "Your claim could not be prepared. Please try again or contact support.",
        }

    logger.info(
        "Prepared claim submission for user '%s' with token '%s'.",
        user_uuid,
        confirmation_token,
    )

    return {
        "success": True,
        "confirmation_token": confirmation_token,
        "expires_at": expires_at.isoformat(),
        "status": "pending",
        "message": (
            "Your claim has been prepared. Please confirm the submission in the UI "
            "to complete your claim."
        ),
    }


async def submit_claim_internal(  # noqa: PLR0913, PLR0917
    policy_number: str,
    claim_type: str,
    amount: float,
    description: str,
    user_uuid: uuid.UUID,
    session: AsyncSession | None = None,
) -> dict[str, Any]:
    """Submit a new OmniCare insurance claim on behalf of the authenticated user.

    This is the internal function that performs the actual database write.
    It is called by the /confirm endpoint after the user has confirmed the
    submission via the frontend.

    Args:
        policy_number: The policyholder's policy number.
        claim_type: Category of the claim.
        amount: Claimed amount in US dollars.
        description: Factual description of the incident.
        user_uuid: UUID of the authenticated user.
        session: Optional existing database session. When provided, the claim
            and embedding job are written to the same transaction and the
            caller is responsible for committing.

            This function never commits a caller-supplied session. Committing here
            would release the ``SELECT ... FOR UPDATE`` row lock that
            ``confirm_claim_submission`` holds on the pending submission, letting a
            second concurrent confirmation read the same row as ``pending`` and insert
            a duplicate claim. It only flushes, so the caller can still mark the
            submission ``completed`` and commit both writes atomically.

    Returns:
        dict: Submission confirmation or error payload.
    """
    try:
        validated = ClaimSubmission(
            policy_number=policy_number,
            claim_type=claim_type,
            amount=amount,
            description=description,
        )
    except ValidationError as exc:
        errors = [
            f"{err['loc'][-1] if err['loc'] else 'field'}: {err['msg']}" for err in exc.errors()
        ]
        logger.warning("Claim submission validation failed: %s", errors)
        return {"success": False, "validation_errors": errors}

    confirmation_id = f"CLM-{uuid.uuid4().hex[:8].upper()}"

    async def _submit(session: AsyncSession) -> dict[str, Any]:
        new_claim = Claim(
            claim_id=confirmation_id,
            policy_number=validated.policy_number,
            claim_type=validated.claim_type,
            status=ClaimStatus.SUBMITTED.value,
            amount=validated.amount,
            description=validated.description,
            owner_id=user_uuid,
        )
        session.add(new_claim)
        await session.flush()

        job = EmbeddingJob(
            claim_uuid=new_claim.id,
            claim_id=new_claim.claim_id,
            owner_id=new_claim.owner_id,
            claim_type=new_claim.claim_type,
            description=new_claim.description,
            policy_number=new_claim.policy_number,
            claim_status=new_claim.status,
            status="pending",
            status_detail="pending",
        )
        session.add(job)
        await session.flush()

        logger.info(
            "Claim '%s' submitted for policy '%s'.",
            confirmation_id,
            validated.policy_number,
        )

        return {
            "success": True,
            "confirmation_id": confirmation_id,
            "status": ClaimStatus.SUBMITTED.value,
            "policy_number": validated.policy_number,
            "claim_type": validated.claim_type,
            "amount": validated.amount,
            "description": validated.description,
            "citation": (
                f"Claim {confirmation_id} recorded in the OmniCare claims system "
                f"under policy {validated.policy_number}."
            ),
            "message": (
                f"Your claim {confirmation_id} has been successfully submitted "
                "and is now being processed. Keep this ID for your records."
            ),
        }

    try:
        if session is None:
            async with async_session_factory() as owned_session:
                result = await _submit(owned_session)
                await owned_session.commit()
                return result
        else:
            return await _submit(session)
    except Exception as exc:
        logger.exception("Failed to persist claim '%s': %s", confirmation_id, exc)
        return {
            "success": False,
            "error": "Your claim could not be saved. Please try again or contact support.",
        }
