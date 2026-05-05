"""Foundation tables for email ingestion via IMAP labels.

Adds:
  - mail_accounts: one per IMAP connection, holds encrypted app password
  - watched_labels: one per (account, label) pair, the unit users add/remove
  - watched_messages: per-message tracking, analog of watched_files
  - documents email-metadata columns: nullable, populated only for email docs

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # mail_accounts
    op.create_table(
        "mail_accounts",
        sa.Column("account_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("imap_host", sa.Text(), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_username", sa.Text(), nullable=False),
        sa.Column("app_password_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("imap_host", "imap_username", name="uq_mail_accounts_host_username"),
    )

    # watched_labels
    op.create_table(
        "watched_labels",
        sa.Column("label_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label_path", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("last_uid_seen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("account_id", "label_path", name="uq_watched_labels_account_path"),
    )

    # watched_messages
    op.create_table(
        "watched_messages",
        sa.Column("message_pk", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "label_id",
            UUID(as_uuid=True),
            sa.ForeignKey("watched_labels.label_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("imap_uid", sa.BigInteger(), nullable=False),
        sa.Column("eml_sha256", sa.LargeBinary(), nullable=False),
        sa.Column(
            "email_doc_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("unlabeled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("label_id", "message_id", name="uq_watched_messages_label_message"),
    )
    op.create_index(
        "ix_watched_messages_label_status",
        "watched_messages",
        ["label_id", "status"],
    )
    op.create_index(
        "ix_watched_messages_eml_sha",
        "watched_messages",
        ["eml_sha256"],
    )

    # documents email-metadata columns
    op.add_column("documents", sa.Column("email_message_id", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("email_thread_id", sa.Text(), nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "email_parent_doc_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("documents", sa.Column("email_from_address", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("email_from_name", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("email_to_addresses", ARRAY(sa.Text()), nullable=True))
    op.add_column("documents", sa.Column("email_cc_addresses", ARRAY(sa.Text()), nullable=True))
    op.add_column("documents", sa.Column("email_date_sent", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("email_label_path", sa.Text(), nullable=True))

    op.create_index(
        "ix_documents_email_message_id",
        "documents",
        ["email_message_id"],
        postgresql_where=sa.text("email_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_documents_email_thread_id",
        "documents",
        ["email_thread_id"],
        postgresql_where=sa.text("email_thread_id IS NOT NULL"),
    )
    op.create_index(
        "ix_documents_email_parent",
        "documents",
        ["email_parent_doc_id"],
        postgresql_where=sa.text("email_parent_doc_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_email_parent", table_name="documents")
    op.drop_index("ix_documents_email_thread_id", table_name="documents")
    op.drop_index("ix_documents_email_message_id", table_name="documents")
    op.drop_column("documents", "email_label_path")
    op.drop_column("documents", "email_date_sent")
    op.drop_column("documents", "email_cc_addresses")
    op.drop_column("documents", "email_to_addresses")
    op.drop_column("documents", "email_from_name")
    op.drop_column("documents", "email_from_address")
    op.drop_column("documents", "email_parent_doc_id")
    op.drop_column("documents", "email_thread_id")
    op.drop_column("documents", "email_message_id")

    op.drop_index("ix_watched_messages_eml_sha", table_name="watched_messages")
    op.drop_index("ix_watched_messages_label_status", table_name="watched_messages")
    op.drop_table("watched_messages")
    op.drop_table("watched_labels")
    op.drop_table("mail_accounts")
