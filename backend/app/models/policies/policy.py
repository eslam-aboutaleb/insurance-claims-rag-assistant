"""
SQLAlchemy ORM model for the ``policies`` table.

Represents a logical policy document (e.g., "omnicare_base") tied to a product
and jurisdiction. A policy has one or more versioned snapshots stored in
``policy_versions``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, DateTime, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Policy(Base):
    """Database model for an OmniCare policy document.

    Attributes:
        policy_id: Primary key UUID.
        product: Product code identifying the policy document set.
        jurisdiction: Jurisdiction code (e.g., "US").
        is_active: Whether this policy is currently active.
        created_at: Timestamp when the policy record was created.
        updated_at: Timestamp when the policy record was last updated.
    """

    __tablename__ = "policies"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )
