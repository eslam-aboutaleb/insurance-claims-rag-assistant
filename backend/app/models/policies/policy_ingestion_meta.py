"""
SQLAlchemy ORM model for the ``policy_ingestion_meta`` table.

Stores a SHA-256 hash of the ingested policy file so that changes to the
source document trigger re-ingestion. This is a lightweight metadata table
used by the policy ingestion idempotency check.
"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PolicyIngestionMeta(Base):
    """Database model for policy ingestion metadata.

    Attributes:
        id: Primary key auto-increment integer.
        source: Absolute path to the policy source file.
        source_hash: SHA-256 hex digest of the file contents at ingestion time.
        ingested_at: Timestamp of the last successful ingestion.
    """

    __tablename__ = "policy_ingestion_meta"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_hash: Mapped[str] = mapped_column(String, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", nullable=False
    )
