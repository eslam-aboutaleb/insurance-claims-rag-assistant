"""SQLAlchemy ORM models for cross-cutting platform tables: the append-only
audit trail and the notification outbox with the read model it fills."""

from app.models.platform.audit_event import AuditEvent
from app.models.platform.notification_job import (
    CHANNEL_EMAIL,
    CHANNEL_IN_APP,
    CHANNEL_SMS,
    CHANNEL_WEBHOOK,
    JOB_TYPE_CLAIM_STATUS_CHANGED,
    JOB_TYPE_CLAIM_SUBMITTED,
    JOB_TYPE_CLAIM_WITHDRAWN,
    MAX_ATTEMPTS,
    NOTIFICATION_CHANNELS,
    NOTIFICATION_JOB_TYPES,
    Notification,
    NotificationJob,
)

__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_IN_APP",
    "CHANNEL_SMS",
    "CHANNEL_WEBHOOK",
    "JOB_TYPE_CLAIM_STATUS_CHANGED",
    "JOB_TYPE_CLAIM_SUBMITTED",
    "JOB_TYPE_CLAIM_WITHDRAWN",
    "MAX_ATTEMPTS",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_JOB_TYPES",
    "AuditEvent",
    "Notification",
    "NotificationJob",
]
