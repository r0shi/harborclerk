"""Add OAuth tables for MCP authorization.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column(
            "client_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("client_secret_hash", sa.Text, nullable=True),
        sa.Column("client_name", sa.Text, nullable=True),
        sa.Column("redirect_uris", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("grant_types", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("response_types", sa.dialects.postgresql.JSONB, nullable=False),
        sa.Column("scope", sa.Text, nullable=False, server_default="mcp"),
        sa.Column("client_uri", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "oauth_codes",
        sa.Column(
            "code_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code_hash", sa.Text, nullable=False),
        sa.Column(
            "client_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oauth_clients.client_id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("redirect_uri", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("code_challenge", sa.Text, nullable=False),
        sa.Column(
            "code_challenge_method",
            sa.Text,
            nullable=False,
            server_default="S256",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "used",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "oauth_tokens",
        sa.Column(
            "token_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("access_token_hash", sa.Text, nullable=False),
        sa.Column("refresh_token_hash", sa.Text, nullable=True),
        sa.Column(
            "client_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("oauth_clients.client_id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column(
            "access_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "refresh_token_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "revoked",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_oauth_tokens_access_hash",
        "oauth_tokens",
        ["access_token_hash"],
    )
    op.create_index(
        "ix_oauth_tokens_refresh_hash",
        "oauth_tokens",
        ["refresh_token_hash"],
    )
    op.create_index(
        "ix_oauth_codes_code_hash",
        "oauth_codes",
        ["code_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_codes_code_hash", table_name="oauth_codes")
    op.drop_index("ix_oauth_tokens_refresh_hash", table_name="oauth_tokens")
    op.drop_index("ix_oauth_tokens_access_hash", table_name="oauth_tokens")
    op.drop_table("oauth_tokens")
    op.drop_table("oauth_codes")
    op.drop_table("oauth_clients")
