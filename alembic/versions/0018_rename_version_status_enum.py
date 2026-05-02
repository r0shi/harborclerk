"""Rename Postgres enum type version_status -> pipeline_status.

The Python class was renamed VersionStatus -> PipelineStatus in PR #255
(Stage 3 flatten), but the underlying Postgres enum type kept its old
name for the duration of the refactor to avoid coupling the Python rename
with a schema migration. This migration brings the DB type name in line
with the Python name so future readers of pg_dump aren't confused.

The Python alias VersionStatus = PipelineStatus is kept for back-compat
with any external tooling that still imports it; that's a separate
concern from the DB type name.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... RENAME TO ... is a metadata-only operation in Postgres
    # (no table rewrite), so this is fast even on large databases.
    op.execute("ALTER TYPE version_status RENAME TO pipeline_status")


def downgrade() -> None:
    op.execute("ALTER TYPE pipeline_status RENAME TO version_status")
