"""create_notification_jobs_and_feed_tables

Revision ID: b6f2c8a4d1e7
Revises: b8c3d9e4f1a6
Create Date: 2026-10-11 08:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6f2c8a4d1e7"
down_revision: str | Sequence[str] | None = "b8c3d9e4f1a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTIFICATION_JOBS_INDEXES = (
    # The claim query filters on status and orders by created_at, so both columns
    # belong in one index rather than one each.
    ("ix_notification_jobs_status_created_at", ["status", "created_at"]),
)

# Declared separately from the composite indexes above because these two answer
# "which jobs are waiting for this user" and "what job types are pending".
_NOTIFICATION_JOBS_EXTRA_INDEXES = (
    ("ix_notification_jobs_user_id", ["user_id"]),
    ("ix_notification_jobs_job_type", ["job_type"]),
)

_NOTIFICATIONS_INDEXES = (
    # The feed read is "this user's notifications, newest first", so the owner and
    # the ordering columns share one index.
    ("ix_notifications_user_id_created_at", ["user_id", "created_at"]),
)

_NOTIFICATIONS_EXTRA_INDEXES = (("ix_notifications_claim_id", ["claim_id"]),)


def _create_indexes_if_missing(
    table_name: str,
    indexes: tuple[tuple[str, list[str]], ...],
) -> None:
    """Create each index unless the database already has it.

    Args:
        table_name: Table the indexes belong to.
        indexes: ``(index name, columns)`` pairs to ensure.
    """
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}
    for name, columns in indexes:
        if name not in existing:
            op.create_index(name, table_name, columns, unique=False)


def _drop_indexes_if_present(
    table_name: str,
    indexes: tuple[tuple[str, list[str]], ...],
) -> None:
    """Drop each index unless the database has already dropped it.

    Args:
        table_name: Table the indexes belong to.
        indexes: ``(index name, columns)`` pairs to remove.
    """
    existing = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}
    for name, _ in indexes:
        if name in existing:
            op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    # Both references are enforced, not merely indexed: a queued notification that
    # outlived its recipient would deliver into a feed nobody owns, and one that
    # outlived its job would sit in the feed with no record of what delivered it.
    if not sa.inspect(connection).has_table("notification_jobs"):
        op.create_table(
            "notification_jobs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            # Text rather than an enum: a new delivery kind must not require a
            # schema change, and the worker's template table is the authority on
            # which values it can render.
            sa.Column("job_type", sa.String(length=64), nullable=False),
            # JSONB rather than a column per job type, for the same reason: the
            # payload is whatever the template for the job type reads.
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column(
                "channel",
                sa.String(length=32),
                nullable=False,
                server_default="in_app",
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("status_detail", sa.String(), nullable=True),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("locked_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="notification_jobs_user_id_fkey",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="notification_jobs_pkey"),
        )

    # Indexes are created outside the table guards and are individually
    # idempotent, so a table the application module already created at runtime
    # still gets its indexes. Guarding the indexes with the tables would leave
    # that path with a complete schema and no indexes at all.
    _create_indexes_if_missing("notification_jobs", _NOTIFICATION_JOBS_INDEXES)
    _create_indexes_if_missing("notification_jobs", _NOTIFICATION_JOBS_EXTRA_INDEXES)

    if not sa.inspect(connection).has_table("notifications"):
        op.create_table(
            "notifications",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("job_type", sa.String(length=64), nullable=False),
            sa.Column(
                "channel",
                sa.String(length=32),
                nullable=False,
                server_default="in_app",
            ),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("body", sa.String(), nullable=False),
            # Plain text rather than a reference: the feed must stay readable for a
            # claim that has since been deleted, and the notification's own text is
            # already a snapshot.
            sa.Column("claim_id", sa.String(), nullable=True),
            sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            # One delivered row per outbox job. A worker that loses its lock
            # mid-pass retries the job, and without this the retry would notify the
            # same claimant a second time.
            sa.UniqueConstraint("job_id", name="uq_notifications_job_id"),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="notifications_user_id_fkey",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["job_id"],
                ["notification_jobs.id"],
                name="notifications_job_id_fkey",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="notifications_pkey"),
        )

    _create_indexes_if_missing("notifications", _NOTIFICATIONS_INDEXES)
    _create_indexes_if_missing("notifications", _NOTIFICATIONS_EXTRA_INDEXES)


def downgrade() -> None:
    """Downgrade schema."""
    connection = op.get_bind()

    # The feed table is the child (it points at notification_jobs), so it is
    # dropped first. Each table is guarded independently: an early return when
    # one is absent would orphan the other and leave the downgrade half-applied.
    if sa.inspect(connection).has_table("notifications"):
        _drop_indexes_if_present("notifications", _NOTIFICATIONS_INDEXES)
        _drop_indexes_if_present("notifications", _NOTIFICATIONS_EXTRA_INDEXES)
        op.drop_table("notifications")

    if sa.inspect(connection).has_table("notification_jobs"):
        _drop_indexes_if_present("notification_jobs", _NOTIFICATION_JOBS_INDEXES)
        _drop_indexes_if_present("notification_jobs", _NOTIFICATION_JOBS_EXTRA_INDEXES)
        op.drop_table("notification_jobs")
