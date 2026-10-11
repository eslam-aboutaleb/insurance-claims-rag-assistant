"""
SQLAlchemy ORM model for the ``users`` table.

Represents an authenticated OmniCare user. Password storage uses Argon2id
hashes, which are intentionally stored in a separate column from the username
to avoid accidental leakage in query logs or admin interfaces.

Every user carries a role, because an API that serves both policyholders and
insurers is only as safe as its weakest authorization check. The default is
``policyholder``: the tier that existed before the column was added, so an
existing row keeps the access it already had and no endpoint changes behaviour
for the accounts it already had.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserRole(str, Enum):
    """The authorization tiers an account can hold.

    Attributes:
        POLICYHOLDER: An end user. May act on their own claims, conversations,
            and ratings, and on nothing else.
        ADMIN: An operator. May read the cross-owner queue, the audit trail, and
            the embedding outbox, and may run the evaluation harness.
        CLAIMS_HANDLER: Reserved for the handler tier that decides a claim's
            fate. No endpoint reads it yet -- the lifecycle route still refuses
            administrative transitions to everyone -- so it exists to keep the
            column's vocabulary complete rather than to grant anything today.
    """

    POLICYHOLDER = "policyholder"
    ADMIN = "admin"
    CLAIMS_HANDLER = "claims_handler"


class User(Base):
    """Database model for an OmniCare user account.

    Attributes:
        id: Primary key UUID.
        username: Unique, indexed username for authentication.
        password_hash: Argon2id hash of the user's password. Never store
            plaintext passwords.
        role: Authorization tier, one of ``UserRole``. Defaults to
            ``policyholder`` at both the ORM and database level, so a row
            written before the column existed and a row inserted by raw SQL
            both land in the tier that changes nothing.
        created_at: Timestamp of account creation, set automatically by the
            database server.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(200), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserRole.POLICYHOLDER.value,
        server_default=UserRole.POLICYHOLDER.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["User", "UserRole"]
