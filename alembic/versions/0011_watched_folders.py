"""Add watched folders tables, ingestion priority, and relax version constraints."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── watched_folders ──
    op.create_table(
        "watched_folders",
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("bookmark_data", sa.LargeBinary(), nullable=False),
        sa.Column("recursive", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_event_id", sa.BigInteger(), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # ── watched_file_status enum ──
    watched_file_status = postgresql.ENUM("active", "removed", name="watched_file_status", create_type=False)
    watched_file_status.create(op.get_bind(), checkfirst=True)

    # ── watched_files ──
    op.create_table(
        "watched_files",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "folder_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("watched_folders.folder_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("bookmark_data", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column(
            "doc_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "status",
            watched_file_status,
            server_default="active",
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("folder_id", "relative_path", name="uq_watched_files_folder_path"),
    )
    op.create_index("ix_watched_files_folder_id", "watched_files", ["folder_id"])
    op.create_index("ix_watched_files_status", "watched_files", ["status"])

    # ── ingestion_jobs.priority ──
    op.add_column(
        "ingestion_jobs",
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.create_index("ix_ingestion_jobs_priority_created", "ingestion_jobs", ["priority", "created_at"])

    # ── Drop UNIQUE on document_versions.original_sha256 (idempotent) ──
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conname = 'document_versions_original_sha256_key'"
            " AND conrelid = 'document_versions'::regclass"
        )
    )
    if result.fetchone():
        op.drop_constraint("document_versions_original_sha256_key", "document_versions", type_="unique")

    # ── Make original_bucket and original_object_key nullable ──
    op.alter_column("document_versions", "original_bucket", existing_type=sa.Text(), nullable=True)
    op.alter_column("document_versions", "original_object_key", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # ── Restore NOT NULL on original_bucket / original_object_key ──
    op.alter_column("document_versions", "original_object_key", existing_type=sa.Text(), nullable=False)
    op.alter_column("document_versions", "original_bucket", existing_type=sa.Text(), nullable=False)

    # ── Restore UNIQUE on original_sha256 ──
    op.create_unique_constraint("document_versions_original_sha256_key", "document_versions", ["original_sha256"])

    # ── Drop priority column and index ──
    op.drop_index("ix_ingestion_jobs_priority_created", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "priority")

    # ── Drop watched tables ──
    op.drop_index("ix_watched_files_status", table_name="watched_files")
    op.drop_index("ix_watched_files_folder_id", table_name="watched_files")
    op.drop_table("watched_files")
    op.drop_table("watched_folders")

    # ── Drop enum ──
    postgresql.ENUM(name="watched_file_status").drop(op.get_bind(), checkfirst=True)
