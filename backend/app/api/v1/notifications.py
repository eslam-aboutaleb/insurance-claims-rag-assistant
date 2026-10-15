"""
In-app notification feed endpoints for the OmniCare backend.

``GET /notifications`` returns the caller's delivered notifications, newest first,
and ``POST /notifications/{id}/read`` marks one of them read. Both are scoped to the
caller's own id, so another user's notification is answered as not found rather than
forbidden — the response must not confirm that the id exists.

The feed is a read model, not the queue: ``notification_jobs`` holds what is waiting
to be delivered, and the drain worker writes a row into ``notifications`` when it
delivers. Nothing here can enqueue, retry, or reorder a delivery, and no endpoint on
the request path performs one, because a claimant waiting on a status change must
not also wait on a delivery.

The request and response models live here rather than in ``app.schemas.models``
because they belong to these two tables and nothing else: the shared module is the
frozen contract surface for chat, claims, and auth.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.security.auth import get_current_user
from app.core.database import get_db
from app.models.platform.notification_job import Notification
from app.schemas.models import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter()


class NotificationItem(BaseModel):
    """One delivered notification, as the recipient's feed shows it.

    Attributes:
        id: Primary key of the delivered notification.
        job_type: Which claim event produced it, so a client can key an icon or a
            grouping on it without parsing the text.
        channel: Which provider delivered it. Only ``in_app`` today.
        title: Rendered subject line.
        body: Rendered message text.
        claim_id: The claim this is about, when the notification has one. Present so
            a "view claim" action can deep link without a second request.
        read_at: When the caller acknowledged it, or NULL while it is still unread.
        created_at: When it was delivered.
    """

    id: uuid.UUID = Field(..., description="Primary key of the delivered notification")
    job_type: str = Field(..., description="Claim event that produced the notification")
    channel: str = Field(..., description="Delivery channel that produced the row")
    title: str = Field(..., description="Rendered subject line")
    body: str = Field(..., description="Rendered message text")
    claim_id: str | None = Field(default=None, description="Claim the notification is about")
    read_at: datetime | None = Field(default=None, description="When it was read, if it was")
    created_at: datetime = Field(..., description="When it was delivered")


class NotificationListResponse(BaseModel):
    """One page of the caller's notifications plus the values that produced it.

    Attributes:
        notifications: The page of delivered notifications, newest first.
        count: Number of notifications in this page.
        limit: Page size that was applied.
        offset: Number of newer notifications that were skipped.
    """

    notifications: list[NotificationItem] = Field(
        default_factory=list, description="The page of notifications, newest first"
    )
    count: int = Field(..., description="Number of notifications in this page")
    limit: int = Field(..., description="Page size that was applied")
    offset: int = Field(..., description="Number of newer notifications that were skipped")


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="List Notifications",
    description=(
        "Returns the authenticated user's delivered notifications, newest first. "
        "Every row is filtered on the caller's user id, so another user's "
        "notification can never appear in the response. Pass unread_only=1 to "
        "receive only the ones still unacknowledged."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "A page of the caller's notifications.",
            "model": NotificationListResponse,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
            "model": ErrorResponse,
        },
    },
)
async def list_notifications(  # noqa: PLR0913
    unread_only: bool = Query(
        default=False,
        description="Return only notifications the caller has not acknowledged yet.",
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=50,
        description="Maximum number of notifications to return.",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of newer notifications to skip.",
    ),
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> NotificationListResponse:
    """List the authenticated user's delivered notifications, newest first.

    The owner predicate is the first thing in the WHERE clause, before the unread
    filter and before the pagination window, so a caller can never trim somebody
    else's feed into their own response by choosing a limit.

    Args:
        unread_only: Whether to return only unacknowledged notifications.
        limit: Page size, defaulting to 20 and capped at 50.
        offset: Number of newest notifications to skip.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        NotificationListResponse: One page of the caller's notifications plus the
        pagination values that produced it.

    Raises:
        HTTPException: 401 if the authenticated user id is not a valid UUID.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    stmt = select(Notification).where(Notification.user_id == user_uuid)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    # id breaks ties so a page ordered on a same-microsecond delivery is still
    # deterministic across repeated requests.
    stmt = stmt.order_by(desc(Notification.created_at), desc(Notification.id))
    stmt = stmt.limit(limit).offset(offset)

    notifications = (await session.execute(stmt)).scalars().all()

    return NotificationListResponse(
        notifications=[_notification_item(row) for row in notifications],
        count=len(notifications),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{notification_id}/read",
    response_model=NotificationItem,
    summary="Mark Notification Read",
    description=(
        "Acknowledges one of the caller's delivered notifications. The update is "
        "scoped to the caller's user id, so another user's notification id is "
        "reported as not found rather than forbidden. Re-reading one that is "
        "already acknowledged succeeds without changing it."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": "The notification as it stands now.",
            "model": NotificationItem,
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Not authenticated.",
            "model": ErrorResponse,
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No notification with that id belongs to the caller.",
            "model": ErrorResponse,
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "The notification id is not a valid UUID.",
            "model": ErrorResponse,
        },
    },
)
async def mark_notification_read(
    notification_id: uuid.UUID,
    current_user_id: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> NotificationItem:
    """Mark one of the caller's notifications as read.

    Args:
        notification_id: The delivered notification to acknowledge.
        current_user_id: The authenticated user's UUID, injected by
            ``get_current_user``.
        session: Async database session.

    Returns:
        NotificationItem: The notification with its new ``read_at``.

    Raises:
        HTTPException: 401 if the authenticated user id is not a valid UUID, 404 if
            the notification does not exist or belongs to another user.
    """
    try:
        user_uuid = uuid.UUID(current_user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid user",
        ) from None

    # SECURITY: Both predicates live in the WHERE clause so a notification id
    # belonging to another user is indistinguishable from one that was never issued.
    notification = (
        await session.execute(
            select(Notification).where(
                Notification.id == notification_id,
                Notification.user_id == user_uuid,
            )
        )
    ).scalar_one_or_none()

    if notification is None:
        logger.warning(
            "Notification '%s' read requested by non-owner '%s'; reported as not found.",
            notification_id,
            current_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    # First acknowledgement wins, so a double tap cannot push the timestamp forward
    # and make an older notification look newer than one that arrived after it.
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        await session.commit()

    return _notification_item(notification)


def _notification_item(row: Notification) -> NotificationItem:
    """Project a delivered notification onto the API's response model.

    Args:
        row: The delivered notification to project.

    Returns:
        NotificationItem: The notification as the feed shows it.
    """
    return NotificationItem(
        id=row.id,
        job_type=row.job_type,
        channel=row.channel,
        title=row.title,
        body=row.body,
        claim_id=row.claim_id,
        read_at=row.read_at,
        created_at=row.created_at,
    )


__all__ = [
    "NotificationItem",
    "NotificationListResponse",
    "list_notifications",
    "mark_notification_read",
    "router",
]
