"""
Answer feedback endpoints for the OmniCare backend.

Captures a rating on one assistant answer: ``POST /feedback`` stores or
revises the caller's rating for a message, and ``DELETE /feedback/{message_id}``
takes it back. Both are scoped to the caller, so a user can only ever rate an
answer that was produced for them.

The request and response models live here rather than in
``app.schemas.models`` because they belong to this table and nothing else: the
shared module is the frozen contract surface for chat, claims, and auth, and a
feedback envelope has no reason to appear in it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.security.auth import get_current_user
from app.core.database import get_db
from app.models.conversations import (
    AnswerFeedback,
    Conversation,
    ConversationMessage,
    FeedbackCategory,
    FeedbackRating,
)
from app.schemas.models import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter()


class AnswerFeedbackRequest(BaseModel):
    """Request schema for rating an assistant answer.

    Attributes:
        message_id: The conversation message being rated.
        rating: Whether the answer was helpful. Restricted to the enum so two
            users' ratings can be counted against each other.
        comment: Optional free-text reason the user typed. Length-capped so a
            paste cannot turn a rating into a document.
        category: Optional structured reason the answer was unhelpful.
    """

    message_id: uuid.UUID = Field(..., description="Conversation message being rated")
    rating: FeedbackRating = Field(..., description="Whether the answer was helpful")
    comment: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional free-text reason the user typed",
    )
    category: FeedbackCategory | None = Field(
        default=None,
        description="Optional structured reason the answer was unhelpful",
    )


class AnswerFeedbackResponse(BaseModel):
    """The rating as it is stored, returned so a client can confirm what landed.

    Attributes:
        id: Primary key of the stored rating.
        message_id: The rated conversation message.
        rating: The stored rating, revised in place when the user changed their
            mind.
        comment: The stored comment, when one was given.
        category: The stored category, when one was given.
        created_at: When the row was written by the database.
    """

    id: uuid.UUID = Field(..., description="Primary key of the stored rating")
    message_id: uuid.UUID = Field(..., description="Conversation message being rated")
    rating: FeedbackRating = Field(..., description="Whether the answer was helpful")
    comment: str | None = Field(
        default=None, description="Free-text reason the user typed, when given"
    )
    category: FeedbackCategory | None = Field(
        default=None, description="Structured reason the answer was unhelpful, when given"
    )
    created_at: datetime = Field(..., description="When the row was written")


@router.post(
    "",
    response_model=AnswerFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Rate an Answer",
    description=(
        "Records or revises the caller's rating on one of their own assistant "
        "answers. A user who rates the same message twice revises the existing "
        "row rather than adding a second one, so the stored counts always "
        "describe one rating per user per answer."
    ),
    responses={
        status.HTTP_201_CREATED: {
            "description": "The rating was stored.",
            "model": AnswerFeedbackResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
            "model": ErrorResponse,
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "The message does not exist or is not the caller's.",
            "model": ErrorResponse,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Invalid rating, invalid category, or oversized comment.",
            "model": ErrorResponse,
        },
    },
)
async def submit_answer_feedback(
    payload: AnswerFeedbackRequest,
    request: Request,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> AnswerFeedbackResponse:
    """Store or revise the caller's rating on one of their own answers.

    Args:
        payload: The message being rated, the rating, and any optional detail.
        request: The request being audited; supplies the client IP and user
            agent for the audit trail entry.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        AnswerFeedbackResponse: The rating as stored after this request.

    Raises:
        HTTPException: 401 if the authenticated user id is not a valid UUID,
            403 if the message does not belong to the caller, 422 if the
            request body fails validation.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    await _require_owned_message(
        session,
        message_id=payload.message_id,
        user_uuid=user_uuid,
    )

    # ON CONFLICT rather than a read-then-write: the second tap on a thumbs
    # button revises the row in one statement, so a double click cannot race a
    # second copy into existence.
    values = insert(AnswerFeedback).values(
        message_id=payload.message_id,
        user_id=user_uuid,
        rating=payload.rating.value,
        comment=payload.comment,
        category=payload.category.value if payload.category else None,
    )
    upsert = values.on_conflict_do_update(
        index_elements=[AnswerFeedback.message_id, AnswerFeedback.user_id],
        set_={
            "rating": values.excluded.rating,
            "comment": values.excluded.comment,
            "category": values.excluded.category,
        },
    )
    await session.execute(upsert)

    stored = (
        await session.execute(
            select(AnswerFeedback).where(
                AnswerFeedback.message_id == payload.message_id,
                AnswerFeedback.user_id == user_uuid,
            )
        )
    ).scalar_one()

    # A rating on a real answer is the product's highest-value quality signal,
    # and it is only ever visible to admins through this trail, so the row that
    # carries it is audited. The single commit below persists the rating and its
    # trail entry together.
    await record_audit(
        session,
        "feedback.submitted",
        "answer_feedback",
        str(payload.message_id),
        after={
            "rating": stored.rating,
            "category": stored.category,
            "comment": stored.comment,
        },
        actor_id=user_uuid,
        request=request,
    )
    await session.commit()

    logger.info(
        "User '%s' rated message '%s' as '%s'.",
        current_user_id,
        payload.message_id,
        stored.rating,
    )

    return AnswerFeedbackResponse(
        id=stored.id,
        message_id=stored.message_id,
        rating=FeedbackRating(stored.rating),
        comment=stored.comment,
        category=FeedbackCategory(stored.category) if stored.category else None,
        created_at=stored.created_at,
    )


@router.delete(
    "/{message_id}",
    # A 204 must not carry a body, and FastAPI otherwise infers one from the
    # ``None`` return annotation, so the response model is closed explicitly.
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw Answer Feedback",
    description=(
        "Removes the caller's rating on one of their own assistant answers. "
        "The message must belong to the caller, and a caller who never rated "
        "the message gets the same empty success as one who just did."
    ),
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "The caller no longer has a rating on the message.",
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
            "model": ErrorResponse,
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "The message does not exist or is not the caller's.",
            "model": ErrorResponse,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "The message id is not a valid UUID.",
            "model": ErrorResponse,
        },
    },
)
async def withdraw_answer_feedback(
    message_id: uuid.UUID,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Remove the caller's rating on one of their own answers.

    The delete is idempotent: a caller who never rated the message still gets a
    204, because there is nothing left to say once the rating is gone and a
    caller is entitled to learn nothing about ratings other than their own.

    Withdrawing is recorded in the audit trail even though the delete is
    idempotent. The trail is the only place the two states are distinguishable:
    once the row is gone, "this user never rated it" and "this user withdrew a
    rating" look identical, so an append-only record made before the delete is
    what preserves the difference.

    Args:
        message_id: The rated conversation message.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Raises:
        HTTPException: 401 if the authenticated user id is not a valid UUID,
            403 if the message does not belong to the caller.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    await _require_owned_message(
        session,
        message_id=message_id,
        user_uuid=user_uuid,
    )

    await record_audit(
        session,
        action="feedback.withdrawn",
        entity_type="answer_feedback",
        entity_id=str(message_id),
        actor_id=user_uuid,
        before={"message_id": str(message_id)},
    )

    await session.execute(
        delete(AnswerFeedback).where(
            AnswerFeedback.message_id == message_id,
            AnswerFeedback.user_id == user_uuid,
        )
    )
    await session.commit()

    logger.info(
        "User '%s' withdrew any rating on message '%s'.",
        current_user_id,
        message_id,
    )


async def _require_owned_message(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    user_uuid: uuid.UUID,
) -> ConversationMessage:
    """Load a message only if the conversation it belongs to is the caller's.

    Both predicates live in the WHERE clause and both failures raise the same
    403, so the endpoint cannot be used to tell whether a message id exists:
    a message belonging to another user is answered exactly like one that was
    never issued.

    Args:
        session: The session the lookup runs in.
        message_id: The message the caller claims to have received.
        user_uuid: The user who must own the message's conversation.

    Returns:
        ConversationMessage: The message, which belongs to the caller.

    Raises:
        HTTPException: 403 when no message with that id belongs to the caller.
    """
    message = (
        await session.execute(
            select(ConversationMessage)
            .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
            .where(
                ConversationMessage.id == message_id,
                Conversation.user_id == user_uuid,
            )
        )
    ).scalar_one_or_none()

    if message is None:
        logger.warning(
            "Feedback requested for message '%s' by non-owner '%s'; reported as forbidden.",
            message_id,
            user_uuid,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This message is not available to you.",
        )

    return message
