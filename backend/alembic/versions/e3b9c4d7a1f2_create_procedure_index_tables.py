"""create_procedure_index_tables

Revision ID: e3b9c4d7a1f2
Revises: c9e1a4b6d8f0
Create Date: 2026-10-11 14:20:31.118447

Creates the procedures index: the ``procedure_chunks`` table the playbooks are
retrieved from, and the ``procedure_versions`` table their ingestion snapshots
live in. It is a second index, not an extension of the policy one, so a policy
re-ingest can never retire a procedure version.

The tsvector trigger is not optional: the hybrid retriever's keyword stage is a
plain ``tsvector @@ plainto_tsquery`` predicate, and without it the full-text
half of every procedure search would silently return nothing.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3b9c4d7a1f2"
down_revision: str | Sequence[str] | None = "c9e1a4b6d8f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "procedure_versions",
        sa.Column(
            "version_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        # The playbook's file name is its identity: the procedures index holds
        # independent files rather than versions of one parent document, so
        # there is no procedure id to hang a foreign key on.
        sa.Column("source_file", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("chunker_version", sa.Text(), nullable=True),
        sa.Column("chunk_size", sa.Integer(), nullable=True),
        sa.Column("overlap", sa.Integer(), nullable=True),
        sa.Column("retrieval_schema_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("version_id"),
    )
    op.create_index(
        "ix_procedure_versions_source_file_effective",
        "procedure_versions",
        ["source_file", "effective_from"],
        unique=False,
    )

    op.create_table(
        "procedure_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.String(128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.String(255), nullable=False),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("sub_chunk_index", sa.Integer(), nullable=False),
        sa.Column("procedure_version_id", sa.UUID(), nullable=False),
        sa.Column("tsvector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.PrimaryKeyConstraint("id", name="procedure_chunks_pkey"),
        # Chunks are owned by the version that wrote them, so retiring a
        # playbook version takes its chunks with it instead of leaving text in
        # the index that no version row explains.
        sa.ForeignKeyConstraint(
            ["procedure_version_id"],
            ["procedure_versions.version_id"],
            name="procedure_chunks_version_id_fkey",
            ondelete="CASCADE",
        ),
    )
    # Chunk ids are positional within one document, so two playbooks both name
    # their opening chunk "chunk_0". Uniqueness is therefore scoped to the
    # source file rather than global, and this composite doubles as the index
    # the ``get_procedure`` retrieval filter needs on ``source`` alone.
    op.create_index(
        "ix_procedure_chunks_source_chunk_id",
        "procedure_chunks",
        ["source", "chunk_id"],
        unique=True,
    )
    op.create_index("ix_procedure_chunks_section", "procedure_chunks", ["section"], unique=False)
    op.create_index(
        "ix_procedure_chunks_tsvector",
        "procedure_chunks",
        ["tsvector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_procedure_chunks_embedding",
        "procedure_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_l2_ops"},
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION procedure_chunks_tsvector_trigger() RETURNS trigger AS $$
        begin
          new.tsvector := to_tsvector('english', coalesce(new.text,''));
          return new;
        end
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER procedure_chunks_tsvectorupdate BEFORE INSERT OR UPDATE
        ON procedure_chunks FOR EACH ROW EXECUTE PROCEDURE procedure_chunks_tsvector_trigger();
    """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER procedure_chunks_tsvectorupdate ON procedure_chunks")
    op.execute("DROP FUNCTION procedure_chunks_tsvector_trigger")
    op.drop_index("ix_procedure_chunks_embedding", table_name="procedure_chunks")
    op.drop_index(
        "ix_procedure_chunks_tsvector", table_name="procedure_chunks", postgresql_using="gin"
    )
    op.drop_index("ix_procedure_chunks_source_chunk_id", table_name="procedure_chunks")
    op.drop_index("ix_procedure_chunks_section", table_name="procedure_chunks")
    op.drop_table("procedure_chunks")
    op.drop_index("ix_procedure_versions_source_file_effective", table_name="procedure_versions")
    op.drop_table("procedure_versions")
