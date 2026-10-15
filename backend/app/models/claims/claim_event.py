"""
SQLAlchemy ORM model for the ``claim_events`` table.

``claims.status`` keeps only the current state, so every transition destroys the
value it replaced: nothing can answer "when did this claim last change, and who
changed it?". This table is the audit trail that survives the overwrite.

The claim lifecycle itself is defined here as well. A transition rule only
holds if the column it writes, the event that records it, and the endpoint that
enforces it all agree on the same set of values, so the status enum and the
legal-transition map live next to the model that persists them rather than
being re-declared in each layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ClaimStatus(str, Enum):
    """The statuses a claim can hold, in the order a claim moves through them.

    Attributes:
        SUBMITTED: The claim was filed and is waiting for a handler.
        UNDER_REVIEW: A claims handler is actively assessing the claim.
        INFO_REQUESTED: A handler needs more from the claimant before deciding.
        APPROVED: The claim was accepted. No further transition exists.
        DENIED: The claim was refused. No further transition exists.
        WITHDRAWN: The claimant pulled the claim back themselves.
        CLOSED: The claim left the handler's queue without a decision.
    """

    SUBMITTED = "Submitted"
    UNDER_REVIEW = "Under Review"
    INFO_REQUESTED = "Info Requested"
    APPROVED = "Approved"
    DENIED = "Denied"
    WITHDRAWN = "Withdrawn"
    CLOSED = "Closed"


CLAIM_STATUSES: tuple[ClaimStatus, ...] = tuple(ClaimStatus)

# The statuses a policyholder may set on their own claim. Everything else
# records a decision only a claims handler can make, so those transitions carry
# an authority check the API enforces before the state machine is consulted.
POLICYHOLDER_STATUSES: frozenset[ClaimStatus] = frozenset({ClaimStatus.WITHDRAWN})

# Legal transitions. A status absent from this map has no successor at all,
# which is how the terminal statuses are expressed.
ALLOWED_STATUS_TRANSITIONS: dict[ClaimStatus, frozenset[ClaimStatus]] = {
    ClaimStatus.SUBMITTED: frozenset({ClaimStatus.UNDER_REVIEW, ClaimStatus.WITHDRAWN}),
    ClaimStatus.UNDER_REVIEW: frozenset(
        {
            ClaimStatus.INFO_REQUESTED,
            ClaimStatus.APPROVED,
            ClaimStatus.DENIED,
            ClaimStatus.WITHDRAWN,
        }
    ),
    # A handler who asks for more documents either receives them and returns
    # the claim to review, or closes it when the claimant never answers.
    ClaimStatus.INFO_REQUESTED: frozenset({ClaimStatus.UNDER_REVIEW, ClaimStatus.CLOSED}),
    ClaimStatus.APPROVED: frozenset(),
    ClaimStatus.DENIED: frozenset(),
    ClaimStatus.WITHDRAWN: frozenset(),
    ClaimStatus.CLOSED: frozenset(),
}


def parse_claim_status(raw: object) -> ClaimStatus | None:
    """Interpret a stored or submitted value as a lifecycle status.

    The comparison is case-insensitive and whitespace-tolerant because the
    column was free text before the lifecycle existed, so a legacy row may
    hold any casing of a known value.

    Args:
        raw: The value to interpret, typically the contents of ``claims.status``.

    Returns:
        ClaimStatus | None: The matching status, or None when the value is not
        part of the claim lifecycle at all.
    """
    if isinstance(raw, ClaimStatus):
        return raw
    normalised = str(raw).strip().casefold()
    for candidate in ClaimStatus:
        if candidate.value.casefold() == normalised:
            return candidate
    return None


def allowed_next_statuses(current: ClaimStatus | None) -> tuple[str, ...]:
    """List the display names a claim in ``current`` may move to.

    Args:
        current: The claim's current status, or None when it is not part of the
            lifecycle.

    Returns:
        tuple[str, ...]: The permitted target statuses, sorted so a caller and
        the API's error message name them in the same order. Empty for a
        terminal or unrecognized status.
    """
    if current is None:
        return ()
    return tuple(target.value for target in sorted(ALLOWED_STATUS_TRANSITIONS[current]))


def is_status_transition_allowed(current: ClaimStatus | None, target: ClaimStatus) -> bool:
    """Report whether moving from ``current`` to ``target`` is a legal transition.

    Args:
        current: The claim's current status, or None when it is not part of the
            lifecycle. None is never a legal starting point, because a claim
            already outside the lifecycle has no defined path forward.
        target: The status being requested.

    Returns:
        bool: True when the transition map permits the move.
    """
    return current is not None and target in ALLOWED_STATUS_TRANSITIONS[current]


class ClaimEvent(Base):
    """Database model for one transition in a claim's lifecycle.

    Attributes:
        id: Primary key UUID.
        claim_uuid: Foreign key to the claim that transitioned. The constraint
            is real, not just an indexed column: an event that outlived its
            claim would put a fabricated history in front of a claim nobody can
            read any more.
        actor_id: Who performed the transition. Nullable so a system-initiated
            change can be recorded without attributing it to a person, and
            released by the database when the actor's account is removed.
        from_status: Status the claim held before the transition, or None for
            the event that files the claim.
        to_status: Status the claim moved to.
        note: Optional free-text reason recorded alongside the transition.
        created_at: Timestamp of the transition, generated by the database so
            that a row written by a worker with a skewed clock still orders
            correctly against its neighbours.
    """

    __tablename__ = "claim_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    claim_uuid: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # The history read filters on claim_uuid and orders by created_at DESC,
        # so both columns belong in one index.
        Index("ix_claim_events_claim_uuid_created_at", claim_uuid, created_at),
    )


__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "CLAIM_STATUSES",
    "POLICYHOLDER_STATUSES",
    "ClaimEvent",
    "ClaimStatus",
    "allowed_next_statuses",
    "is_status_transition_allowed",
    "parse_claim_status",
]
