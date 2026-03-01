"""Add entities pipeline stage: version_status and job_stage enum values, entities table.

Revision ID: 0012
Revises: 0011
Create Date: 2026-02-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction in PostgreSQL.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("ALTER TYPE version_status ADD VALUE IF NOT EXISTS 'extracting_entities' BEFORE 'embedding'"))
    op.execute(sa.text("ALTER TYPE version_status ADD VALUE IF NOT EXISTS 'entities_done' BEFORE 'embedding'"))
    op.execute(sa.text("ALTER TYPE job_stage ADD VALUE IF NOT EXISTS 'entities' BEFORE 'embed'"))
    op.execute(sa.text("BEGIN"))

    # Create entities table
    op.create_table(
        "entities",
        sa.Column(
            "entity_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "version_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_versions.version_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chunk_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chunks.chunk_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "doc_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_text", sa.Text, nullable=False),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("start_char", sa.Integer, nullable=False),
        sa.Column("end_char", sa.Integer, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_index("ix_entities_version_id", "entities", ["version_id"])
    op.create_index("ix_entities_chunk_id", "entities", ["chunk_id"])
    op.create_index("ix_entities_doc_id", "entities", ["doc_id"])
    op.create_index("ix_entities_type_text", "entities", ["entity_type", "entity_text"])
    # GIN trigram index for ILIKE substring search
    op.execute("CREATE INDEX ix_entities_text_trgm ON entities USING gin (entity_text gin_trgm_ops)")


def downgrade() -> None:
    op.drop_table("entities")
    # PostgreSQL does not support removing enum values; they remain harmless.
