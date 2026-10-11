"""create_claim_events_table

Revision ID: f3c8a1d7b2e9
Revises: d8e9f0a1b2c3
Create Date: 2026-10-11 05:02:11.884213

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3c8a1d7b2e9"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "claim_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_uuid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # The claim reference is enforced, not merely indexed: an event that
        # outlived its claim would present a fabricated history to a claim
        # nobody can read.
        sa.ForeignKeyConstraint(
            ["claim_uuid"],
            ["claims.id"],
            name="claim_events_claim_uuid_fkey",
            ondelete="CASCADE",
        ),
        # The actor reference is released rather than enforced, because a
        # system transition records no actor and removing an account must not
        # delete the history of what it did.
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name="claim_events_actor_id_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="claim_events_pkey"),
    )
    op.create_index("ix_claim_events_claim_uuid", "claim_events", ["claim_uuid"], unique=False)
    op.create_index(
        "ix_claim_events_claim_uuid_created_at",
        "claim_events",
        ["claim_uuid", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_claim_events_claim_uuid_created_at", table_name="claim_events")
    op.drop_index("ix_claim_events_claim_uuid", table_name="claim_events")
    op.drop_table("claim_events")
