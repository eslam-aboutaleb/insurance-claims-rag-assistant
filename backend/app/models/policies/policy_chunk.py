"""
SQLAlchemy ORM model for the ``policy_chunks`` table.

Represents a single chunk of an ingested insurance policy document. Each
chunk is stored with its text content, section metadata, a computed
``tsvector`` column for PostgreSQL full-text search, and a ``embedding`` column
for pgvector hybrid retrieval.
"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, ForeignKey, Index, Integer, String, Text, UUID
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.models.base import Base


class PolicyChunk(Base):
    """Database model for a chunk of an ingested policy document.

    Attributes:
        id: Primary key UUID.
        chunk_id: Unique string identifier for the chunk (e.g., "policy_chunk_0").
        text: The raw text content of the policy chunk.
        section: The Markdown section header this chunk belongs to
            (e.g., "Home Water Damage Coverage").
        source: The source filename the chunk was extracted from.
        chunk_index: Zero-based index of the chunk within the document.
        sub_chunk_index: Zero-based index of the sub-chunk within a section.
        tsvector: PostgreSQL tsvector for PostgreSQL full-text search over the chunk text.
        embedding: pgvector embedding of the chunk text for hybrid retrieval.
    """

    __tablename__ = "policy_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chunk_id = Column(String(128), unique=True, nullable=False, index=True)
    text = Column(Text, nullable=False)
    section = Column(String(255), nullable=False, index=True)
    source = Column(String(255), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    sub_chunk_index = Column(Integer, nullable=False)
    policy_id = Column(
        UUID(as_uuid=True),
        ForeignKey("policies.policy_id", ondelete="CASCADE"),
        nullable=False,
    )
    policy_version_id = Column(
        UUID(as_uuid=True),
        ForeignKey("policy_versions.version_id", ondelete="CASCADE"),
        nullable=False,
    )
    tsvector = Column(TSVECTOR, nullable=True)
    embedding = Column(Vector(1536), nullable=False)

    __table_args__ = (
        Index("ix_policy_chunks_tsvector", tsvector, postgresql_using="gin"),
        Index(
            "ix_policy_chunks_embedding",
            embedding,
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_l2_ops"},
        ),
    )
