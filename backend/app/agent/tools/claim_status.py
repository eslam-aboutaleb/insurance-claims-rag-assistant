"""
Claim status lookup tool for the OmniCare agent.

Reads from Postgres to find claim information securely linked to the current user.
The lookup is scoped to the authenticated user's ``owner_id`` to prevent
horizontal privilege escalation (IDOR attacks).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.agent.context import current_user_id
from app.core.database import async_session_factory
from app.models.claims.claim import Claim

logger = logging.getLogger(__name__)


async def get_claim_status(claim_id: str, policy_number: str = "") -> dict[str, Any]:
    """Look up the status of an existing OmniCare insurance claim by its ID.

    Use this tool when a user asks about the status, progress, or details of
    a specific claim. The claim ID is required. The policy number is optional
    and currently unused but reserved for future enhanced filtering.

    The lookup is scoped to the authenticated user via ``current_user_id``,
    ensuring that users can only retrieve their own claim information.

    Args:
        claim_id: The unique claim identifier provided by the user.
            Example: "CLM-8821"
        policy_number: The policyholder's policy number. Currently unused
            but accepted for forward compatibility.

    Returns:
        dict: Claim status details. On success, includes ``found=True``,
        ``claim_id``, ``policy_number``, ``claim_type``, ``status``,
        ``amount``, and ``citation``. On failure, includes ``found=False``
        and an ``error`` message.
    """
    try:
        user_uuid = current_user_id.get()
    except LookupError:
        logger.error("current_user_id not found in context.")
        return {"found": False, "error": "Unauthorized claim lookup."}

    # Normalize the claim ID to uppercase to handle case-insensitive input
    # while matching the stored format.
    normalised_id = claim_id.strip().upper()

    try:
        async with async_session_factory() as session:
            # SECURITY: The WHERE clause includes both claim_id and owner_id.
            # This dual filter prevents IDOR attacks where an attacker supplies
            # a valid claim ID belonging to another user.
            stmt = select(Claim).where(Claim.claim_id == normalised_id, Claim.owner_id == user_uuid)
            result = await session.execute(stmt)
            claim = result.scalar_one_or_none()

            if not claim:
                logger.warning(
                    "IDOR attempt or claim not found: claim %s requested by user %s",
                    normalised_id,
                    user_uuid,
                )
                return {
                    "found": False,
                    "error": (
                        f"No claim found with ID '{claim_id}'. "
                        "Please double-check the claim ID and try again, "
                        "or contact OmniCare support at 1-800-OMNICARE."
                    ),
                }

            logger.info("Claim '%s' found securely for owner '%s'.", normalised_id, user_uuid)
            return {
                "found": True,
                "claim_id": claim.claim_id,
                "policy_number": claim.policy_number,
                "claim_type": claim.claim_type,
                "status": claim.status,
                "amount": claim.amount,
                "citation": (
                    f"Claim {claim.claim_id} in the OmniCare claims system "
                    f"(policy {claim.policy_number})."
                ),
            }
    except Exception as exc:
        logger.exception("Database error during claim lookup: %s", exc)
        return {
            "found": False,
            "error": "Unable to retrieve claims at this time. Please contact support.",
        }
