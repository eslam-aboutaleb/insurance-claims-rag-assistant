"""create_eval_runs_table

Revision ID: f1a4b7c2d9e3
Revises: e3b9c4d7a1f2
Create Date: 2026-10-11 12:41:08.913552

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a4b7c2d9e3"
down_revision: str | Sequence[str] | None = "e3b9c4d7a1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVAL_RUNS_INDEXES = (
    # The trend view reads "the last runs, newest first" and nothing else, so
    # this index is on the ordering column alone.
    ("ix_eval_runs_created_at", ["created_at"]),
    ("ix_eval_runs_git_sha", ["git_sha"]),
    ("ix_eval_runs_mode", ["mode"]),
)


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    # Guarded rather than a bare create_table(): this module can also create the
    # table itself on its first write, so a deployment that has already been
    # persisting runs would otherwise fail the migration it no longer needs --
    # and would then never gain the indexes below.
    if not sa.inspect(connection).has_table("eval_runs"):
        op.create_table(
            "eval_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False),
            # Provenance is half the value of a metric: a trend whose points
            # cannot be tied to a commit is decoration. Nullable because an
            # exported source tree has no .git and a run against it is still
            # worth recording.
            sa.Column("git_sha", sa.String(length=64), nullable=True),
            # JSONB rather than a column per metric: the aggregate set changes
            # whenever a floor is added, and a schema change per floor would make
            # every threshold change a migration.
            sa.Column("config", postgresql.JSONB(), nullable=False),
            sa.Column("metrics", postgresql.JSONB(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            # Denormalised from the metrics payload so a trend query counts the
            # samples behind a point without unpacking JSONB to do it.
            sa.Column("sample_count", sa.Integer(), server_default="0", nullable=False),
            sa.PrimaryKeyConstraint("id", name="eval_runs_pkey"),
        )

    inspector = sa.inspect(connection)
    existing = {index["name"] for index in inspector.get_indexes("eval_runs")}
    for name, columns in _EVAL_RUNS_INDEXES:
        if name not in existing:
            op.create_index(name, "eval_runs", columns, unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # The same idempotence, for the opposite reason: dropping a table the module
    # recreates on its next write would lose the history the migration protects,
    # and dropping an index that is not there fails the downgrade.
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("eval_runs"):
        return

    existing = {index["name"] for index in sa.inspect(connection).get_indexes("eval_runs")}
    for name, _ in reversed(_EVAL_RUNS_INDEXES):
        if name in existing:
            op.drop_index(name, table_name="eval_runs")
    op.drop_table("eval_runs")
