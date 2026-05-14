"""Add users.password_changed_at for JWT revocation on password change.

After this column exists, the auth layer can reject access/refresh tokens
whose `iat` is older than the user's `password_changed_at`. Default
`now()` for existing rows so pre-existing tokens are NOT retroactively
revoked at migration time — only tokens issued before the next password
change are killed.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-13

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")
