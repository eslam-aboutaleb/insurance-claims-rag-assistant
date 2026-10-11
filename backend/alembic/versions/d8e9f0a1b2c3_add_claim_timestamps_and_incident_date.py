"""add_claim_timestamps_and_incident_date

Revision ID: d8e9f0a1b2c3
Revises: c4d5e6f7a8b9
Create Date: 2026-10-11 09:12:44.118203

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "claims",
        sa.Column(
            "incident_date",
            sa.Date(),
            nullable=True,
        ),
    )
    op.add_column(
        "claims",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "claims",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_claims_owner_id_created_at",
        "claims",
        ["owner_id", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_claims_owner_id_created_at", table_name="claims")
    op.drop_column("claims", "updated_at")
    op.drop_column("claims", "created_at")
    op.drop_column("claims", "incident_date")
