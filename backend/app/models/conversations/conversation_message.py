"""
SQLAlchemy ORM model for the ``conversation_messages`` table.

Represents a single message within a chat conversation. Normalized from
the previous JSONB ``messages`` array in the ``conversations`` table to
enable efficient querying, appending, and prevent unbounded document
growth.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationMessage(Base):
    """Database model for a single message in a conversation.

    Attributes:
        id: Primary key UUID.
        conversation_id: Foreign key to the parent conversation.
        role: Message role (e.g., 'user', 'assistant', 'system').
        content: The message text content.
        timestamp: When the message was created.
        metadata: Optional JSONB metadata (sources, tool_calls, etc.).
    """

    __tablename__ = "conversation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    message_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
