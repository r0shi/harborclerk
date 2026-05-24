"""document_links

Adds the document_links table for Phase 4 of the markdown-handling feature.
Each row is a parsed wikilink ([[Note Name]]) from one document to another
(or to a target that doesn't exist yet — target_doc_id is nullable).

Revision ID: 0002_document_links
Revises: 0001_initial
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_document_links"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# These four module-level names are read by alembic via attribute access
# (``module.revision`` etc.) rather than ordinary imports, so static
# analysis (CodeQL ``py/unused-global-variable``) doesn't see them as used.
# Declaring them in ``__all__`` marks them as the module's intentional
# public surface and silences the false positive.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    op.create_table(
        "document_links",
        sa.Column("link_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("src_doc_id", sa.UUID(), nullable=False),
        sa.Column("target_doc_id", sa.UUID(), nullable=True),
        sa.Column("link_text", sa.Text(), nullable=False),
        sa.Column("target_title", sa.Text(), nullable=False),
        sa.Column("anchor", sa.Text(), nullable=True),
        sa.Column("alias", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["src_doc_id"], ["documents.doc_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_doc_id"], ["documents.doc_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("link_id"),
    )
    op.create_index("ix_document_links_src_doc_id", "document_links", ["src_doc_id"])
    op.create_index("ix_document_links_target_doc_id", "document_links", ["target_doc_id"])
    op.create_index("ix_document_links_target_title", "document_links", ["target_title"])


def downgrade() -> None:
    op.drop_index("ix_document_links_target_title", table_name="document_links")
    op.drop_index("ix_document_links_target_doc_id", table_name="document_links")
    op.drop_index("ix_document_links_src_doc_id", table_name="document_links")
    op.drop_table("document_links")
