"""
The audit trail: one append-only row per high-value action, and the hook that
writes the claim-lifecycle half of it.

Two call paths live here on purpose:

  * ``record_audit`` is what a route calls directly, inside the transaction
    that performs the audited action, so the entry commits with it.
    Authentication events and feedback submissions are recorded this way.
  * A ``ClaimEvent`` flush listener covers the claim lifecycle (submission,
    status transitions, withdrawal). That half is recorded from the model layer
    rather than the route because the claim lifecycle is owned by code this
    module must not reach into, and a listener also catches a transition written
    by any future caller -- a trail a future rewrite of the route could bypass
    is not a trail.

Both paths share one guarantee: an audit write that fails is logged and
swallowed. An outage of the trail must not take the business action down with
it, which means an entry can be missing during an incident -- the same trade
every append-only log makes, and the reason this table is not a foreign key to
success.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.agent.context import current_user_id
from app.models.audit_event import AuditEvent
from app.models.claim import Claim
from app.models.claim_event import ClaimEvent, ClaimStatus
from app.observability.request_context import get_request_id

logger = logging.getLogger(__name__)

# A user agent is a header the caller controls, so it is capped before it can
# become a stored document; the truncated prefix is what distinguishes agents
# in practice, so nothing that matters is lost.
_MAX_USER_AGENT_LENGTH = 512


def _request_scoped_identity(
    actor_id: uuid.UUID | None,
    request: Request | None,
) -> tuple[uuid.UUID | None, str | None, str | None]:
    """Resolve the actor, client IP, and user agent for one entry.

    The actor falls back to the ``current_user_id`` context variable when the
    caller does not name one, so an instrumented endpoint that already
    authenticated the request does not have to thread the id through the call.

    Args:
        actor_id: The actor the caller named, if any.
        request: The request being audited, if there is one.

    Returns:
        tuple[uuid.UUID | None, str | None, str | None]: The actor, the client
        IP, and the user agent, each None when it is not knowable.
    """
    resolved_actor = actor_id
    if resolved_actor is None:
        # ``current_user_id`` is only set inside an authenticated request; a
        # startup path or a worker sees LookupError and records a system actor.
        try:
            resolved_actor = current_user_id.get()
        except LookupError:
            resolved_actor = None

    ip_address: str | None = None
    user_agent: str | None = None
    if request is not None:
        ip_address = request.client.host if request.client is not None else None
        raw_user_agent = request.headers.get("user-agent")
        if raw_user_agent is not None:
            user_agent = raw_user_agent[:_MAX_USER_AGENT_LENGTH]

    return resolved_actor, ip_address, user_agent


def _audit_values(  # noqa: PLR0913 - one field per column an entry carries
    action: str,
    entity_type: str,
    entity_id: str | None,
    *,
    before: dict | None,
    after: dict | None,
    actor_id: uuid.UUID | None,
    request: Request | None,
) -> dict:
    """Assemble the column values of one audit row, filling in the request context.

    Args:
        action: The verb performed, e.g. ``"auth.signin"``.
        entity_type: The kind of thing acted on, e.g. ``"user"``.
        entity_id: The identifier of the thing acted on, if known.
        before: Fields as they were, or None when nothing was overwritten.
        after: Fields as they are now, or None.
        actor_id: The actor the caller named, if any.
        request: The request being audited, if there is one.

    Returns:
        dict: The values for an ``AuditEvent`` row.
    """
    resolved_actor, ip_address, user_agent = _request_scoped_identity(actor_id, request)
    return {
        "actor_id": resolved_actor,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "before": before,
        "after": after,
        "ip_address": ip_address,
        "user_agent": user_agent,
        # The correlation id ties this row to its own line in the application
        # log; empty string (no request in context) is stored as NULL.
        "correlation_id": get_request_id() or None,
    }


async def record_audit(  # noqa: PLR0913 - one field per column an entry carries
    session: AsyncSession,
    action: str,
    entity_type: str,
    entity_id: str | None,
    *,
    before: dict | None = None,
    after: dict | None = None,
    actor_id: uuid.UUID | None = None,
    request: Request | None = None,
) -> None:
    """Append one entry to the audit trail, never disturbing the caller.

    The row is written inside a SAVEPOINT of the caller's transaction, so the
    caller's own commit persists both together: an entry can no more outlive the
    action failing than the action can fail because the entry did. Call this
    before the commit you were going to make anyway -- no second transaction,
    and nothing new to forget.

    A write that fails rolls back to its own savepoint, which is why the
    caller's session is safe whatever happens here: no rollback of the caller's
    transaction, no expired objects, no poisoned connection.

    Args:
        session: The session performing the audited action.
        action: The verb performed, e.g. ``"auth.signin"``.
        entity_type: The kind of thing acted on, e.g. ``"user"``.
        entity_id: The identifier of the thing acted on, if known.
        before: Fields as they were, or None when nothing was overwritten.
        after: Fields as they are now, or None.
        actor_id: The actor, when the caller can name one. Falls back to the
            authenticated user in context.
        request: The request being audited, when one is in scope. Supplies the
            client IP and user agent.

    Returns:
        None: This function never raises. A failed write is logged at warning
        level and the request that produced it continues.
    """
    try:
        values = _audit_values(
            action,
            entity_type,
            entity_id,
            before=before,
            after=after,
            actor_id=actor_id,
            request=request,
        )
        async with session.begin_nested():
            await session.execute(insert(AuditEvent).values(**values))
    except Exception:
        logger.warning(
            "Audit trail entry for '%s' on '%s' could not be written.",
            action,
            entity_type,
            exc_info=True,
        )


def _claim_event_action(from_status: str | None, to_status: str) -> str:
    """Name the audit verb a lifecycle event deserves.

    The filing event that opens a claim's history is a submission, the claimant
    pulling their own claim back is a withdrawal, and everything in between is
    a handler decision. Three verbs rather than one, because "the status
    changed" is the question an auditor asks last and never first.

    Args:
        from_status: The status the claim left, or None for the filing event.
        to_status: The status the claim moved to.

    Returns:
        str: ``"claim.submitted"``, ``"claim.withdrawn"``, or
        ``"claim.status_changed"``.
    """
    if from_status is None:
        return "claim.submitted"
    if to_status == ClaimStatus.WITHDRAWN.value:
        return "claim.withdrawn"
    return "claim.status_changed"


def _claim_labels(session: Session, events: list[ClaimEvent]) -> dict[uuid.UUID, str]:
    """Resolve the human-readable claim id behind each lifecycle event.

    ``claim_events`` stores the claim's primary key, but the trail is only
    usable if a human can search it with the claim id they were given, so the
    label is resolved here. The lookup runs inside the flush, where the session
    serves it from the database it is already writing to.

    Args:
        session: The session flushing the events.
        events: The lifecycle events being audited.

    Returns:
        dict[uuid.UUID, str]: Claim primary key to claim id, for every claim
        that could be resolved. A claim that cannot be read is absent, and its
        event is recorded against the primary key instead.
    """
    labels: dict[uuid.UUID, str] = {}
    for event_row in events:
        if event_row.claim_uuid in labels:
            continue
        try:
            claim = session.get(Claim, event_row.claim_uuid)
        except Exception:
            logger.warning("Could not resolve a claim label for the audit trail.", exc_info=True)
            continue
        if claim is not None:
            labels[event_row.claim_uuid] = claim.claim_id
    return labels


def _queue_claim_audit_rows(session: Session, flushed_events: list[ClaimEvent]) -> None:
    """Queue one audit row per lifecycle event flushed in this session.

    Rows are added to the same session rather than written directly, so they
    join the audited transaction: a transition that commits leaves its entry,
    and one that rolls back leaves nothing. This runs inside a flush, where no
    ``await`` is possible, which is why the add is synchronous.

    Args:
        session: The session whose flush produced the events.
        flushed_events: The lifecycle events this flush wrote.
    """
    claim_labels = _claim_labels(session, flushed_events)
    for flushed_event in flushed_events:
        entity_id = claim_labels.get(flushed_event.claim_uuid, str(flushed_event.claim_uuid))
        # The request's IP and user agent are not reachable from a flush, where
        # no request object is in scope; the actor is, because the lifecycle
        # records it on the event itself.
        session.add(
            AuditEvent(
                **_audit_values(
                    _claim_event_action(flushed_event.from_status, flushed_event.to_status),
                    "claim",
                    entity_id,
                    before={"status": flushed_event.from_status}
                    if flushed_event.from_status
                    else None,
                    after={"status": flushed_event.to_status},
                    actor_id=flushed_event.actor_id,
                    request=None,
                )
            )
        )


def _after_flush(session: Session, flush_context: Any) -> None:
    """Queue audit rows for every lifecycle event a flush wrote.

    Args:
        session: The session that finished flushing.
        flush_context: SQLAlchemy's internal flush context, unused.
    """
    try:
        flushed_events = [obj for obj in session.new if isinstance(obj, ClaimEvent)]
    except Exception:
        logger.warning("Could not inspect flushed claim events for the audit trail.", exc_info=True)
        return

    if not flushed_events:
        return

    try:
        _queue_claim_audit_rows(session, flushed_events)
    except Exception:
        logger.warning("Audit trail rows for claim events could not be queued.", exc_info=True)


# Listening on the Session class rather than one sessionmaker catches the
# request sessions, the worker sessions, and the application's own startup
# work alike: a lifecycle event should not be auditable only when it happens
# inside a route handler.
event.listen(Session, "after_flush", _after_flush)
