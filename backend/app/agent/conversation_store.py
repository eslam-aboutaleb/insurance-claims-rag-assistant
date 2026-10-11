"""
Persistence helpers for chat turns and their conversation threads.

A turn is persisted transactionally on its own session: the conversation is
located by ``session_id`` (created if it does not yet exist) and a matched pair
of user + assistant ``ConversationMessage`` rows is appended. The two message
ids are generated here rather than by the database so they can be handed back to
the caller: the chat and streaming endpoints echo them in their response frames
and the frontend adopts them in place of any locally-generated id. The feedback
endpoint authorises on these ids, so the id a client rates must be the id the
database actually persisted -- not a client-side guess that matches nothing.

The save is fire-and-forget from the caller's perspective: every exception is
caught and logged so a persistence failure can never break the chat response.
When it fails, the returned :class:`SavedMessageIds` carries no ids, which the
callers render as ``null`` in the response while still returning the answer.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from app.database import async_session_factory
from app.models.conversation import Conversation
from app.models.conversation_message import ConversationMessage

logger = logging.getLogger(__name__)

_TITLE_MAX_LENGTH = 50


@dataclass(frozen=True)
class SavedMessageIds:
    """The primary keys the backend assigned to a persisted turn's messages.

    Attributes:
        user_message_id: The persisted user message's id, or ``None`` when the
            turn could not be saved.
        assistant_message_id: The persisted assistant answer's id, or ``None``
            when the turn could not be saved. This is the id the feedback
            endpoint authorises against, so a client rates this message.
    """

    user_message_id: str | None = None
    assistant_message_id: str | None = None


async def save_conversation_turn(  # noqa: PLR0913
    user_id: str,
    session_id: str,
    message: str,
    response_text: str,
    sources: list[str],
    tool_calls: list[dict[str, object]],
) -> SavedMessageIds:
    """Persist a chat turn and report the ids the database assigned to it.

    The conversation is looked up by ``session_id`` and created on first sight,
    using the first characters of the user's message as a title. Two message
    ids are generated up front and passed explicitly to their
    ``ConversationMessage`` rows, so the same values are both stored and
    returned: the caller can tell the client the id the backend will accept for
    feedback, and the two can never drift apart the way a client-generated id
    and a database default would.

    Args:
        user_id: The authenticated user's id (UUID string).
        session_id: The agent session id that groups this conversation's turns.
        message: The user's message text.
        response_text: The assistant's complete answer text.
        sources: The citations the answer was built from.
        tool_calls: The tool activity recorded for the turn.

    Returns:
        SavedMessageIds: The user and assistant message ids, or an instance
            with both set to ``None`` when persistence failed (the failure is
            logged, never raised).
    """
    try:
        async with async_session_factory() as db_session:
            conv = (
                await db_session.execute(
                    select(Conversation).where(Conversation.session_id == session_id)
                )
            ).scalar_one_or_none()

            if conv is None:
                title = message[:_TITLE_MAX_LENGTH] or "New conversation"
                conv = Conversation(
                    user_id=uuid.UUID(user_id),
                    session_id=session_id,
                    title=title,
                )
                db_session.add(conv)
                await db_session.flush()

            user_message_id = uuid.uuid4()
            assistant_message_id = uuid.uuid4()

            user_message = ConversationMessage(
                id=user_message_id,
                conversation_id=conv.id,
                role="user",
                content=message,
                timestamp=datetime.now(UTC),
                message_metadata=None,
            )
            assistant_message = ConversationMessage(
                id=assistant_message_id,
                conversation_id=conv.id,
                role="assistant",
                content=response_text,
                timestamp=datetime.now(UTC),
                message_metadata={
                    "sources": sources or [],
                    "tool_calls": tool_calls or [],
                },
            )

            db_session.add(user_message)
            db_session.add(assistant_message)
            await db_session.commit()

            return SavedMessageIds(
                user_message_id=str(user_message_id),
                assistant_message_id=str(assistant_message_id),
            )
    except Exception as exc:
        logger.error("Failed to save conversation turn: %s", exc, exc_info=True)
        return SavedMessageIds()
