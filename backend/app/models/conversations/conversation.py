"""
SQLAlchemy ORM model for the ``conversations`` table.

Represents a single chat conversation between a user and the OmniCare agent.
Messages are stored as a JSONB array to preserve the original structure
(role, content, timestamps, sources, tool calls) without requiring a separate
message table.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.models.base import Base


class Conversation(Base):
    """Database model for a chat conversation.

    Attributes:
        id: Primary key UUID.
        user_id: Foreign key to the user who owns this conversation.
        session_id: Unique string identifier used by the ADK agent runtime
            to track the in-memory session. Distinct from the database PK.
        title: Human-readable conversation title, defaulting to "New Chat".
        messages: JSONB array of message objects. Each message contains at
            least ``id``, ``role``, ``content``, and ``timestamp``, plus
            optional ``sources`` and ``tool_calls`` fields.
        created_at: Timestamp of conversation creation.
        updated_at: Timestamp of last message or metadata update.
    """

    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    session_id = Column(String(128), unique=True, index=True, nullable=False)
    title = Column(String(255), nullable=False, default="New Chat")
    # JSONB is used instead of a normalized messages table because:
    #   1. Messages are always read/written together with the conversation.
    #   2. The schema is append-only and managed by the agent runtime.
    #   3. It simplifies queries for full conversation history retrieval.
    messages = Column(JSONB, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
