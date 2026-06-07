"""add pipeline generation to ingestion jobs

Revision ID: 0007_ingestion_job_pipeline_seq
Revises: 0006_backfill_mime_type
Create Date: 2026-06-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_ingestion_job_pipeline_seq"
down_revision: str | None = "0006_backfill_mime_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("pipeline_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.execute(
        """
        UPDATE ingestion_jobs AS job
        SET pipeline_seq = COALESCE(doc.pipeline_seq, 0)
        FROM documents AS doc
        WHERE doc.doc_id = job.doc_id
        """
    )


def downgrade() -> None:
    op.drop_column("ingestion_jobs", "pipeline_seq")
