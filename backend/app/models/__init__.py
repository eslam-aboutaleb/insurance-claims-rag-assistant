"""
SQLAlchemy ORM models for the OmniCare backend, grouped by bounded context.

The flat module list this package used to carry is now grouped under the
contexts the rest of the application already uses:

  - ``User`` (``user.py``)      -- identity; every other table references it.
  - ``app.models.claims``      -- claims, their lifecycle events, the pending
    submission awaiting confirmation, and the embedding outbox.
  - ``app.models.conversations`` -- conversations, their normalized messages,
    and the feedback ratings attached to assistant answers.
  - ``app.models.policies``    -- the ingested corpus: policies, their
    versioned snapshots and chunks, ingestion bookkeeping, and the versioned
    procedure playbooks.
  - ``app.models.platform``    -- cross-cutting system tables: the append-only
    audit trail and the notification outbox and feed.

Importing this package imports every model module, which is what registers all
tables on ``Base.metadata``. Alembic's ``env.py`` and ``create_all_tables``
therefore only need ``import app.models`` and cannot drift as models are added.

Model modules never import from this package -- they import ``Base`` from
``app.models.base`` directly -- so the registry can import them without a
circular dependency.
"""

from app.models.base import Base
from app.models.claims import (
    ALLOWED_STATUS_TRANSITIONS,
    CLAIM_STATUSES,
    POLICYHOLDER_STATUSES,
    Claim,
    ClaimEvent,
    ClaimStatus,
    ClaimSubmission,
    EmbeddingJob,
    allowed_next_statuses,
    is_status_transition_allowed,
    parse_claim_status,
)
from app.models.conversations import (
    AnswerFeedback,
    Conversation,
    ConversationMessage,
    FeedbackCategory,
    FeedbackRating,
)
from app.models.platform import (
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
    AuditEvent,
    Notification,
    NotificationJob,
)
from app.models.policies import (
    Policy,
    PolicyChunk,
    PolicyIngestionMeta,
    PolicyVersion,
    ProcedureVersion,
)
from app.models.user import User, UserRole

__all__ = [
    "ALLOWED_STATUS_TRANSITIONS",
    "CHANNEL_EMAIL",
    "CHANNEL_IN_APP",
    "CHANNEL_SMS",
    "CHANNEL_WEBHOOK",
    "CLAIM_STATUSES",
    "JOB_TYPE_CLAIM_STATUS_CHANGED",
    "JOB_TYPE_CLAIM_SUBMITTED",
    "JOB_TYPE_CLAIM_WITHDRAWN",
    "MAX_ATTEMPTS",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_JOB_TYPES",
    "POLICYHOLDER_STATUSES",
    "AnswerFeedback",
    "AuditEvent",
    "Base",
    "Claim",
    "ClaimEvent",
    "ClaimStatus",
    "ClaimSubmission",
    "Conversation",
    "ConversationMessage",
    "EmbeddingJob",
    "FeedbackCategory",
    "FeedbackRating",
    "Notification",
    "NotificationJob",
    "Policy",
    "PolicyChunk",
    "PolicyIngestionMeta",
    "PolicyVersion",
    "ProcedureVersion",
    "User",
    "UserRole",
    "allowed_next_statuses",
    "is_status_transition_allowed",
    "parse_claim_status",
]
