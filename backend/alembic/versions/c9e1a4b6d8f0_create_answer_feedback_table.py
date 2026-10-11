"""create_answer_feedback_table

Revision ID: c9e1a4b6d8f0
Revises: f3c8a1d7b2e9
Create Date: 2026-10-11 12:14:33.481920

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9e1a4b6d8f0"
down_revision: str | Sequence[str] | None = "f3c8a1d7b2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "answer_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rating", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Both references are enforced, not merely indexed: a rating that
        # outlived its answer would describe text nobody can read, and one that
        # outlived its user would attribute a rating to an account that is gone.
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["conversation_messages.id"],
            name="answer_feedback_message_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="answer_feedback_user_id_fkey",
            ondelete="CASCADE",
        ),
        # The pair is the row's identity: a user who changes their mind revises
        # this row, and the upsert keys on the pair rather than racing a second
        # one into existence.
        sa.UniqueConstraint(
            "message_id",
            "user_id",
            name="uq_answer_feedback_message_user",
        ),
        sa.PrimaryKeyConstraint("id", name="answer_feedback_pkey"),
    )
    op.create_index(
        "ix_answer_feedback_message_id",
        "answer_feedback",
        ["message_id"],
        unique=False,
    )
    op.create_index("ix_answer_feedback_user_id", "answer_feedback", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_answer_feedback_user_id", table_name="answer_feedback")
    op.drop_index("ix_answer_feedback_message_id", table_name="answer_feedback")
    op.drop_table("answer_feedback")
