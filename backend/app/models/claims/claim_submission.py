"""
SQLAlchemy ORM model for the ``claim_submissions`` table.

Represents a pending claim submission awaiting user confirmation via the
two-step confirmation flow. This prevents the agent from unilaterally
submitting claims on behalf of the user.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ClaimSubmission(Base):
    """Database model for a pending claim submission awaiting confirmation.

    Attributes:
        id: Primary key UUID.
        user_id: Foreign key to the user who initiated the submission.
        confirmation_token: Unique token returned to the frontend for confirmation.
        claim_data: JSONB blob of the pending claim fields.
        status: Current status (pending, completed, expired, cancelled).
        expires_at: When this pending submission expires.
        created_at: Timestamp of creation.
    """

    __tablename__ = "claim_submissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    confirmation_token: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    claim_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
