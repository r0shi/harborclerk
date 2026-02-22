"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-02-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Extensions
    # pgcrypto is optional — gen_random_uuid() is built-in on PG 13+
    op.execute("DO $$ BEGIN CREATE EXTENSION IF NOT EXISTS pgcrypto; EXCEPTION WHEN OTHERS THEN NULL; END $$")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # Enum types
    user_role = postgresql.ENUM("admin", "user", name="user_role", create_type=False)
    user_role.create(op.get_bind(), checkfirst=True)

    upload_source = postgresql.ENUM("web", "watch_folder", name="upload_source", create_type=False)
    upload_source.create(op.get_bind(), checkfirst=True)

    version_status = postgresql.ENUM(
        "queued", "extracting", "extracted",
        "ocr_running", "ocr_done",
        "chunking", "chunked",
        "embedding", "embedded",
        "ready", "error",
        name="version_status", create_type=False,
    )
    version_status.create(op.get_bind(), checkfirst=True)

    job_stage = postgresql.ENUM(
        "extract", "ocr", "chunk", "embed", "finalize",
        name="job_stage", create_type=False,
    )
    job_stage.create(op.get_bind(), checkfirst=True)

    job_status = postgresql.ENUM(
        "queued", "running", "done", "error",
        name="job_status", create_type=False,
    )
    job_status.create(op.get_bind(), checkfirst=True)

    # ── users ──
    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", user_role, nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )
    op.execute("ALTER TABLE users ALTER COLUMN email TYPE CITEXT")
    op.create_unique_constraint("uq_users_email", "users", ["email"])

    # ── api_keys ──
    op.create_table(
        "api_keys",
        sa.Column("key_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("key_hash", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("api_keys_active_idx", "api_keys", ["is_active"])

    # ── documents ──
    op.create_table(
        "documents",
        sa.Column("doc_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("canonical_filename", sa.Text),
        sa.Column("latest_version_id", postgresql.UUID),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute("CREATE INDEX documents_updated_idx ON documents (updated_at DESC)")

    # ── uploads ──
    op.create_table(
        "uploads",
        sa.Column("upload_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID, sa.ForeignKey("users.user_id", ondelete="SET NULL")),
        sa.Column("source", upload_source, nullable=False, server_default="web"),
        sa.Column("original_filename", sa.Text, nullable=False),
        sa.Column("mime_type", sa.Text),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("sha256", sa.LargeBinary),
        sa.Column("minio_bucket", sa.Text, nullable=False),
        sa.Column("minio_object_key", sa.Text, nullable=False),
        sa.Column("doc_id", postgresql.UUID, sa.ForeignKey("documents.doc_id", ondelete="SET NULL")),
        sa.Column("version_id", postgresql.UUID),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute("CREATE INDEX uploads_created_idx ON uploads (created_at DESC)")

    # ── document_versions ──
    op.create_table(
        "document_versions",
        sa.Column("version_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("doc_id", postgresql.UUID, sa.ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_sha256", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("original_bucket", sa.Text, nullable=False),
        sa.Column("original_object_key", sa.Text, nullable=False),
        sa.Column("mime_type", sa.Text),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("status", version_status, nullable=False, server_default="queued"),
        sa.Column("error", sa.Text),
        sa.Column("has_text_layer", sa.Boolean),
        sa.Column("needs_ocr", sa.Boolean),
        sa.Column("extracted_chars", sa.BigInteger, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute("CREATE INDEX versions_doc_created_idx ON document_versions (doc_id, created_at DESC)")
    op.create_index("versions_status_idx", "document_versions", ["status"])

    # ── document_pages ──
    op.create_table(
        "document_pages",
        sa.Column("page_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("version_id", postgresql.UUID, sa.ForeignKey("document_versions.version_id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_num", sa.Integer, nullable=False),
        sa.Column("page_text", sa.Text, nullable=False, server_default=""),
        sa.Column("ocr_used", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("ocr_confidence", sa.Float),
    )
    op.execute(
        "ALTER TABLE document_pages ADD COLUMN char_count INT "
        "GENERATED ALWAYS AS (char_length(page_text)) STORED"
    )
    op.create_unique_constraint("uq_pages_version_page", "document_pages", ["version_id", "page_num"])
    op.create_index("pages_version_page_idx", "document_pages", ["version_id", "page_num"])

    # ── chunks ──
    op.create_table(
        "chunks",
        sa.Column("chunk_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("version_id", postgresql.UUID, sa.ForeignKey("document_versions.version_id", ondelete="CASCADE"), nullable=False),
        sa.Column("doc_id", postgresql.UUID, sa.ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_num", sa.Integer, nullable=False),
        sa.Column("page_start", sa.Integer),
        sa.Column("page_end", sa.Integer),
        sa.Column("char_start", sa.Integer),
        sa.Column("char_end", sa.Integer),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("language", sa.Text, nullable=False, server_default="english"),
        sa.Column("ocr_used", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("ocr_confidence", sa.Float),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_chunks_version_num", "chunks", ["version_id", "chunk_num"])

    # Generated tsvector columns + embedding via raw SQL
    op.execute(
        "ALTER TABLE chunks ADD COLUMN fts_en TSVECTOR "
        "GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED"
    )
    op.execute(
        "ALTER TABLE chunks ADD COLUMN fts_fr TSVECTOR "
        "GENERATED ALWAYS AS (to_tsvector('french', coalesce(chunk_text, ''))) STORED"
    )
    op.execute("ALTER TABLE chunks ADD COLUMN embedding vector(384)")

    op.create_index("chunks_doc_idx", "chunks", ["doc_id"])
    op.create_index("chunks_version_idx", "chunks", ["version_id"])
    op.execute("CREATE INDEX chunks_fts_en_idx ON chunks USING GIN(fts_en)")
    op.execute("CREATE INDEX chunks_fts_fr_idx ON chunks USING GIN(fts_fr)")
    op.execute("CREATE INDEX chunks_embedding_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops)")

    # ── ingestion_jobs ──
    op.create_table(
        "ingestion_jobs",
        sa.Column("job_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("version_id", postgresql.UUID, sa.ForeignKey("document_versions.version_id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage", job_stage, nullable=False),
        sa.Column("status", job_status, nullable=False, server_default="queued"),
        sa.Column("progress_current", sa.Integer, server_default=sa.text("0")),
        sa.Column("progress_total", sa.Integer, server_default=sa.text("0")),
        sa.Column("metrics", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_unique_constraint("uq_jobs_version_stage", "ingestion_jobs", ["version_id", "stage"])
    op.create_index("jobs_status_idx", "ingestion_jobs", ["status"])

    # ── audit_log ──
    op.create_table(
        "audit_log",
        sa.Column("audit_id", postgresql.UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID, sa.ForeignKey("users.user_id", ondelete="SET NULL")),
        sa.Column("api_key_id", postgresql.UUID, sa.ForeignKey("api_keys.key_id", ondelete="SET NULL")),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target_type", sa.Text),
        sa.Column("target_id", postgresql.UUID),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute("CREATE INDEX audit_created_idx ON audit_log (created_at DESC)")


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("ingestion_jobs")
    op.drop_table("chunks")
    op.drop_table("document_pages")
    op.drop_table("document_versions")
    op.drop_table("uploads")
    op.drop_table("documents")
    op.drop_table("api_keys")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS job_status")
    op.execute("DROP TYPE IF EXISTS job_stage")
    op.execute("DROP TYPE IF EXISTS version_status")
    op.execute("DROP TYPE IF EXISTS upload_source")
    op.execute("DROP TYPE IF EXISTS user_role")
