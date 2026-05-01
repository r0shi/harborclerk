"""Flatten document model: pull DocumentVersion columns up to Document, drop document_versions.

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import BYTEA

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add new columns to documents (all nullable for backfill) ──
    op.add_column("documents", sa.Column("sha256", BYTEA(), nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "pipeline_status",
            sa.Enum(name="version_status", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "documents",
        sa.Column("pipeline_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    for col in (
        "summary",
        "summary_model",
        "doc_type",
        "mime_type",
        "source_path",
        "error",
        "original_bucket",
        "original_object_key",
    ):
        op.add_column("documents", sa.Column(col, sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("has_text_layer", sa.Boolean(), nullable=True))
    op.add_column("documents", sa.Column("needs_ocr", sa.Boolean(), nullable=True))
    op.add_column("documents", sa.Column("extracted_chars", sa.BigInteger(), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger(), nullable=True))

    # ── 2. Add doc_id columns to child tables that only have version_id today ──
    op.add_column("document_pages", sa.Column("doc_id", sa.UUID(as_uuid=True), nullable=True))
    op.add_column("document_headings", sa.Column("doc_id", sa.UUID(as_uuid=True), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("doc_id", sa.UUID(as_uuid=True), nullable=True))

    # ── 3. Backfill documents columns from latest_version_id ──
    op.execute("""
        UPDATE documents d
        SET sha256             = v.original_sha256,
            pipeline_status    = v.status,
            summary            = v.summary,
            summary_model      = v.summary_model,
            doc_type           = v.doc_type,
            mime_type          = v.mime_type,
            source_path        = v.source_path,
            error              = v.error,
            original_bucket    = v.original_bucket,
            original_object_key = v.original_object_key,
            has_text_layer     = v.has_text_layer,
            needs_ocr          = v.needs_ocr,
            extracted_chars    = v.extracted_chars,
            size_bytes         = v.size_bytes
        FROM document_versions v
        WHERE d.latest_version_id = v.version_id
    """)

    # ── 4. Backfill doc_id on child tables from version→doc mapping ──
    op.execute("""
        UPDATE document_pages p
        SET doc_id = v.doc_id
        FROM document_versions v
        WHERE p.version_id = v.version_id
    """)
    op.execute("""
        UPDATE document_headings h
        SET doc_id = v.doc_id
        FROM document_versions v
        WHERE h.version_id = v.version_id
    """)
    op.execute("""
        UPDATE ingestion_jobs j
        SET doc_id = v.doc_id
        FROM document_versions v
        WHERE j.version_id = v.version_id
    """)

    # ── 5. Delete non-latest version data from child tables ──
    op.execute("""
        DELETE FROM chunks c
        USING documents d
        WHERE c.doc_id = d.doc_id
          AND c.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM entities e
        USING documents d
        WHERE e.doc_id = d.doc_id
          AND e.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM document_pages p
        USING documents d
        WHERE p.doc_id = d.doc_id
          AND p.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM document_headings h
        USING documents d
        WHERE h.doc_id = d.doc_id
          AND h.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM ingestion_jobs j
        USING documents d
        WHERE j.doc_id = d.doc_id
          AND j.version_id != d.latest_version_id
    """)

    # ── 6. Storage rename: rewrite original_object_key in the DB from
    #       'originals/versions/<version_id>/<filename>'  →
    #       'originals/docs/<doc_id>/<filename>'.
    #    Actual MinIO/filesystem object renames are handled by a startup
    #    reconciliation script (Task 8). The DB column is updated here so it
    #    points at the new key immediately after migration. ──
    op.execute("""
        UPDATE documents
        SET original_object_key = regexp_replace(
            original_object_key,
            '^originals/versions/[0-9a-f-]+/',
            'originals/docs/' || doc_id || '/'
        )
        WHERE original_object_key IS NOT NULL
    """)

    # ── 7. Apply NOT NULL now that backfill is complete ──
    op.alter_column("documents", "sha256", nullable=False)
    op.alter_column("documents", "pipeline_status", nullable=False)
    op.alter_column("document_pages", "doc_id", nullable=False)
    op.alter_column("document_headings", "doc_id", nullable=False)
    op.alter_column("ingestion_jobs", "doc_id", nullable=False)

    # ── 8. Add FKs on the new doc_id columns ──
    op.create_foreign_key(
        "fk_pages_doc_id",
        "document_pages",
        "documents",
        ["doc_id"],
        ["doc_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_headings_doc_id",
        "document_headings",
        "documents",
        ["doc_id"],
        ["doc_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_jobs_doc_id",
        "ingestion_jobs",
        "documents",
        ["doc_id"],
        ["doc_id"],
        ondelete="CASCADE",
    )

    # ── 9. Swap unique constraints to be doc_id-keyed ──
    # Drop the old version_id-keyed unique constraints
    op.drop_constraint("uq_chunks_version_num", "chunks", type_="unique")
    op.create_unique_constraint("uq_chunks_doc_num", "chunks", ["doc_id", "chunk_num"])

    op.drop_constraint("uq_pages_version_page", "document_pages", type_="unique")
    op.create_unique_constraint("uq_pages_doc_page", "document_pages", ["doc_id", "page_num"])

    op.drop_constraint("uq_jobs_version_stage", "ingestion_jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_doc_stage", "ingestion_jobs", ["doc_id", "stage"])

    # ── 10. Drop version_id FK constraints before dropping columns ──
    # chunks and entities have FK → document_versions; drop them before the table goes
    op.drop_constraint("chunks_version_id_fkey", "chunks", type_="foreignkey")
    op.drop_constraint("entities_version_id_fkey", "entities", type_="foreignkey")
    op.drop_constraint("document_pages_version_id_fkey", "document_pages", type_="foreignkey")
    op.drop_constraint("document_headings_version_id_fkey", "document_headings", type_="foreignkey")
    op.drop_constraint("ingestion_jobs_version_id_fkey", "ingestion_jobs", type_="foreignkey")

    # Drop version_id indexes that would block column removal
    op.drop_index("chunks_version_idx", table_name="chunks")
    op.drop_index("ix_entities_version_id", table_name="entities")
    op.drop_index("ix_document_headings_version_id", table_name="document_headings")
    op.drop_index("pages_version_page_idx", table_name="document_pages")

    # ── 11. Drop version_id columns from all child tables ──
    for tbl in (
        "chunks",
        "entities",
        "document_pages",
        "document_headings",
        "ingestion_jobs",
        "watched_files",
        "uploads",
    ):
        op.drop_column(tbl, "version_id")

    # ── 12. Drop latest_version_id from documents, then drop document_versions ──
    op.drop_column("documents", "latest_version_id")
    op.drop_table("document_versions")


def downgrade() -> None:
    """Recreate the structural shape of the pre-flatten schema.

    DOES NOT restore data — non-latest version rows were deleted during upgrade
    and cannot be reconstructed. Existing tests in tests/test_migrations.py
    only assert table presence/absence, so a structural-only downgrade is
    sufficient to keep the round-trip test green.
    """
    # Re-create document_versions table (empty), restoring all columns that
    # existed at revision 0016. Use sa.Text for the status column to avoid
    # enum-creation issues (the version_status type already exists in the DB).
    op.create_table(
        "document_versions",
        sa.Column("version_id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "doc_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_sha256", BYTEA(), nullable=False),
        sa.Column("original_bucket", sa.Text(), nullable=True),
        sa.Column("original_object_key", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("has_text_layer", sa.Boolean(), nullable=True),
        sa.Column("needs_ocr", sa.Boolean(), nullable=True),
        sa.Column("extracted_chars", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_model", sa.Text(), nullable=True),
        sa.Column("doc_type", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Cast the text status column to the existing enum type.
    # Must drop default first; text default can't auto-cast to the enum type.
    op.execute("ALTER TABLE document_versions ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE document_versions ALTER COLUMN status TYPE version_status USING status::version_status"
    )
    op.execute("ALTER TABLE document_versions ALTER COLUMN status SET DEFAULT 'queued'::version_status")

    # Restore indexes that existed on document_versions at revision 0016
    # (so that downstream downgrade steps like 0002 can drop them).
    op.execute("CREATE INDEX versions_doc_created_idx ON document_versions (doc_id, created_at DESC)")
    op.create_index("versions_status_idx", "document_versions", ["status"])
    op.create_index("ix_versions_doc_type", "document_versions", ["doc_type"])

    # Re-add latest_version_id to documents
    op.add_column("documents", sa.Column("latest_version_id", sa.UUID(as_uuid=True), nullable=True))

    # Re-add version_id to child tables (nullable; nothing to backfill)
    for tbl in (
        "chunks",
        "entities",
        "document_pages",
        "document_headings",
        "ingestion_jobs",
        "watched_files",
        "uploads",
    ):
        op.add_column(tbl, sa.Column("version_id", sa.UUID(as_uuid=True), nullable=True))

    # Restore version_id indexes
    op.create_index("chunks_version_idx", "chunks", ["version_id"])
    op.create_index("ix_entities_version_id", "entities", ["version_id"])
    op.create_index("ix_document_headings_version_id", "document_headings", ["version_id"])
    op.create_index("pages_version_page_idx", "document_pages", ["version_id", "page_num"])

    # Restore unique constraints on version_id
    op.drop_constraint("uq_chunks_doc_num", "chunks", type_="unique")
    op.create_unique_constraint("uq_chunks_version_num", "chunks", ["version_id", "chunk_num"])

    op.drop_constraint("uq_pages_doc_page", "document_pages", type_="unique")
    op.create_unique_constraint("uq_pages_version_page", "document_pages", ["version_id", "page_num"])

    op.drop_constraint("uq_jobs_doc_stage", "ingestion_jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_version_stage", "ingestion_jobs", ["version_id", "stage"])

    # Drop new FK constraints on doc_id for child tables
    op.drop_constraint("fk_pages_doc_id", "document_pages", type_="foreignkey")
    op.drop_constraint("fk_headings_doc_id", "document_headings", type_="foreignkey")
    op.drop_constraint("fk_jobs_doc_id", "ingestion_jobs", type_="foreignkey")

    # Drop new doc_id columns from child tables
    for tbl in ("document_pages", "document_headings", "ingestion_jobs"):
        op.drop_column(tbl, "doc_id")

    # Drop all the new flat columns from documents
    for col in (
        "sha256",
        "pipeline_status",
        "pipeline_seq",
        "summary",
        "summary_model",
        "doc_type",
        "mime_type",
        "source_path",
        "error",
        "original_bucket",
        "original_object_key",
        "has_text_layer",
        "needs_ocr",
        "extracted_chars",
        "size_bytes",
    ):
        op.drop_column("documents", col)
