"""Add topic modeling tables and document.topic_id."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "corpus_topics",
        sa.Column("topic_id", sa.Integer(), primary_key=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("keywords", ARRAY(sa.Text()), nullable=False),
        sa.Column("doc_count", sa.Integer(), nullable=False),
        sa.Column("representative_doc_ids", ARRAY(UUID(as_uuid=True)), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "corpus_topics_meta",
        sa.Column("id", sa.Integer(), primary_key=True, server_default="1"),
        sa.Column("last_computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("corpus_hash", sa.Text(), nullable=True),
    )
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT 1 FROM information_schema.columns WHERE table_name='documents' AND column_name='topic_id'")
    )
    if not result.fetchone():
        op.add_column(
            "documents",
            sa.Column(
                "topic_id",
                sa.Integer(),
                sa.ForeignKey("corpus_topics.topic_id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("documents", "topic_id")
    op.drop_table("corpus_topics_meta")
    op.drop_table("corpus_topics")
