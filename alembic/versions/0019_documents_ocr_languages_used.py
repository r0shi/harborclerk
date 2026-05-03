"""Add documents.ocr_languages_used to record which languages a doc was OCR'd with.

Population happens in worker/stages/ocr.py after a successful OCR pass.
NULL is used to mean "we don't know" — that covers legacy docs OCR'd
before this column existed, plus docs whose OCR is yet to run.

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("ocr_languages_used", ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "ocr_languages_used")
