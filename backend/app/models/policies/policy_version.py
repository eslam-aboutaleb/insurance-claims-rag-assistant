"""
SQLAlchemy ORM model for the ``policy_versions`` table.

Represents a versioned snapshot of a policy document. Each version is tied to
a ``policies`` row and tracks the effective date range, source hash, and
version identifier.
"""

from __future__ import annotations

import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PolicyVersion(Base):
    """Database model for a versioned policy document snapshot.

    Attributes:
        version_id: Primary key UUID.
        policy_id: Foreign key to the parent ``policies`` row.
        version: Human-readable version identifier (e.g., "current").
        effective_from: Timestamp when this version became effective.
        effective_to: Optional timestamp when this version was superseded.
            NULL means the version is current.
        source_hash: SHA-256 hash of the source document at ingestion time.
        embedding_model: Name of the embedding model used for this version.
        embedding_dim: Dimension of the embeddings used for this version.
        chunker_version: Version identifier for the chunking strategy.
        chunk_size: Number of words per chunk.
        overlap: Number of overlapping words between chunks.
        retrieval_schema_version: Version of the retrieval schema.
        created_at: Timestamp when the version record was created.
        created_by: Optional identifier for the user/system that created the version.
    """

    __tablename__ = "policy_versions"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.policy_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunker_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieval_schema_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
