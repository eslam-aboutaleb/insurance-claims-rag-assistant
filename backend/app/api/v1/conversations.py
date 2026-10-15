"""
Conversation history endpoints.

GET /api/v1/chat/conversations - List the authenticated user's conversations.
GET /api/v1/chat/conversations/{id} - Retrieve a single conversation's history.
PATCH /api/v1/chat/conversations/{id} - Rename a conversation.
DELETE /api/v1/chat/conversations/{id} - Delete a conversation and its messages.

Security:
  - All endpoints require authentication via the ``get_current_user``
    dependency.
  - Conversation queries are scoped to the authenticated user's ``user_id``
    to prevent horizontal privilege escalation (users cannot access other
    users' conversation history).
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.agent import delete_user_session
from app.security.auth import get_current_user
from app.core.database import get_db
from app.models.conversations.conversation import Conversation
from app.models.conversations.conversation_message import ConversationMessage
from app.schemas.models import (
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMetadata,
    ConversationRenameRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _load_owned_conversation(
    session: AsyncSession,
    conversation_id: str,
    current_user_id: str,
) -> Conversation:
    """Load a conversation only when it belongs to the authenticated user.

    Both predicates live in the WHERE clause and every failure raises the same
    404, so a conversation belonging to somebody else is answered exactly like
    one that was never created: the endpoints cannot be used to learn whether
    an id exists.

    Args:
        session: Async database session.
        conversation_id: The conversation id as it arrived on the request path.
        current_user_id: The authenticated user's UUID.

    Returns:
        Conversation: The conversation row owned by the caller.

    Raises:
        HTTPException: 400 if the conversation id is not a UUID, 404 if no
            conversation with that id belongs to the caller.
    """
    try:
        conv_uuid = uuid.UUID(conversation_id)
    except ValueError as err:
        raise HTTPException(status_code=400, detail="Invalid conversation ID") from err

    conv = (
        await session.execute(
            select(Conversation)
            .where(Conversation.id == conv_uuid)
            .where(Conversation.user_id == uuid.UUID(current_user_id))
        )
    ).scalar_one_or_none()

    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conv


@router.get(
    "/chat/conversations",
    response_model=ConversationListResponse,
    summary="List Conversations",
    description=("Returns metadata for all conversations belonging to the authenticated user."),
)
async def list_conversations(
    q: str | None = Query(
        default=None,
        max_length=100,
        description=(
            "Case-insensitive substring the conversation title must contain. "
            "Omit it to list every conversation."
        ),
    ),
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConversationListResponse:
    """List all conversations for the authenticated user.

    Retrieves conversation metadata (id, title, timestamps) ordered by most
    recently updated. The message history is not included to keep the response
    lightweight for sidebar list views.

    Args:
        q: Optional case-insensitive substring the title must contain. When it
            is omitted or empty the filter is skipped entirely, so the
            unfiltered response is unchanged.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        ConversationListResponse: A list of conversation metadata objects
        including id, title, and timestamps, ordered by most recently updated.
    """
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == uuid.UUID(current_user_id))
        .order_by(desc(Conversation.updated_at))
    )
    if q:
        stmt = stmt.where(Conversation.title.ilike(f"%{q}%"))
    result = await session.execute(stmt)
    conversations = result.scalars().all()

    return ConversationListResponse(
        conversations=[
            {
                "id": c.id,
                "title": c.title,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in conversations
        ]
    )


@router.get(
    "/chat/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    summary="Get Conversation History",
    description=(
        "Returns the full message history for a specific conversation, ensuring the user owns it."
    ),
)
async def get_conversation(
    conversation_id: str,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConversationDetailResponse:
    """Retrieve the full history of a single conversation.

    Validates that the conversation belongs to the authenticated user before
    returning it, preventing horizontal privilege escalation.

    Args:
        conversation_id: The UUID of the conversation to retrieve.

    Returns:
        ConversationDetailResponse: The conversation metadata plus the full
        list of messages.

    Raises:
        HTTPException: 400 if the conversation ID is malformed, 404 if the
        conversation does not exist or does not belong to the user.
    """
    conv = await _load_owned_conversation(session, conversation_id, current_user_id)

    stmt_messages = (
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.timestamp)
    )
    result_messages = await session.execute(stmt_messages)
    db_messages = result_messages.scalars().all()

    messages = []
    for db_message in db_messages:
        message_metadata = db_message.message_metadata or {}
        messages.append(
            {
                "id": str(db_message.id),
                "role": db_message.role,
                "content": db_message.content,
                "timestamp": db_message.timestamp.isoformat(),
                "sources": message_metadata.get("sources") or [],
                "tool_calls": message_metadata.get("tool_calls") or [],
            }
        )

    return ConversationDetailResponse(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=messages,
    )


@router.patch(
    "/chat/conversations/{conversation_id}",
    response_model=ConversationMetadata,
    summary="Rename Conversation",
    description=(
        "Updates the title of a specific conversation, ensuring the user owns it. "
        "The title is stored trimmed and must be between 1 and 255 characters."
    ),
)
async def rename_conversation(
    conversation_id: str,
    payload: ConversationRenameRequest,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ConversationMetadata:
    """Rename a conversation owned by the authenticated user.

    The request schema trims the title, so what is stored is what the user
    sees rather than the string they typed.

    Args:
        conversation_id: The UUID of the conversation to rename.
        payload: The new title, validated to be 1-255 characters.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        ConversationMetadata: The conversation metadata after the rename,
        carrying the timestamp the update assigned.

    Raises:
        HTTPException: 400 if the conversation ID is malformed, 404 if the
            conversation does not exist or does not belong to the user.
    """
    conv = await _load_owned_conversation(session, conversation_id, current_user_id)

    conv.title = payload.title
    await session.commit()

    logger.info(
        "User '%s' renamed conversation '%s'.",
        current_user_id,
        conv.id,
    )

    return ConversationMetadata(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


@router.delete(
    "/chat/conversations/{conversation_id}",
    # A 204 must not carry a body, and FastAPI otherwise infers one from the
    # ``None`` return annotation, so the response model is closed explicitly.
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Conversation",
    description=(
        "Deletes a specific conversation together with its messages, ensuring the user owns it."
    ),
)
async def delete_conversation(
    conversation_id: str,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> None:
    """Delete a conversation and its messages.

    The message rows are deleted explicitly rather than relying on the
    ``ON DELETE CASCADE`` the model declares, so the behaviour does not depend
    on the FK definition a given migration happened to apply. Both deletes
    share the transaction that commits them, so a failure leaves the
    conversation intact.

    The conversation's agent session is released once the row is gone: the
    runner would otherwise keep resuming -- and citing -- a transcript the user
    has already deleted on the next message.

    Args:
        conversation_id: The UUID of the conversation to delete.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Raises:
        HTTPException: 400 if the conversation ID is malformed, 404 if the
            conversation does not exist or does not belong to the user.
    """
    conv = await _load_owned_conversation(session, conversation_id, current_user_id)

    await session.execute(
        delete(ConversationMessage).where(ConversationMessage.conversation_id == conv.id)
    )
    await session.execute(delete(Conversation).where(Conversation.id == conv.id))
    await session.commit()

    await delete_user_session(current_user_id, conv.session_id)

    logger.info(
        "User '%s' deleted conversation '%s'.",
        current_user_id,
        conv.id,
    )
