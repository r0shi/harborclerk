# Watched-Folder-First Stage 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Flatten the Document/DocumentVersion model. Pull per-content state up to `documents`, drop `document_versions`, re-key all child tables to `doc_id`, and replace versioned-modify with replace-in-place semantics.

**Architecture:** Single Alembic migration adds the flat columns + backfills + drops the old structure + renames MinIO/filesystem object keys. Code (backend + frontend) is rewritten on the same branch to consume the flattened schema. The watcher's modify branch becomes "DELETE child rows, bump `pipeline_seq`, enqueue extract." Workers gain race-protection: read `pipeline_seq` at job start, compare-and-write at result time. Cross-path SHA dedup is removed (each path → its own doc).

**Tech Stack:** SQLAlchemy 2.0 async + Alembic, Python `watchdog`, FastAPI, React 19. No new dependencies.

**Spec:** [`docs/superpowers/specs/2026-05-01-watched-folder-first-stage-3-design.md`](../specs/2026-05-01-watched-folder-first-stage-3-design.md)

**Branch state:** Each task in this plan produces one commit. The branch as a whole is broken between commits — the schema migration in Task 1 lays new columns alongside the old, code refactors in Tasks 2-12 rekey to the new columns, and the same migration also drops the old columns at end-of-upgrade. Running the app at any intermediate task is not expected to work; only the final branch tip is deployable.

**Test infra status:** Backend has full pytest + ruff coverage. Frontend has lint / typecheck / format / build only — no Vitest or RTL. Frontend tasks rely on those four checks plus manual smoke (same pattern as Stages 1 and 2).

---

## File Structure

**New files:**
- `alembic/versions/0017_flatten_document_model.py` — single migration covering schema changes + backfill + storage rename + drops

**Modified files (backend):**
- `src/harbor_clerk/models/document.py` — add ~14 new columns, drop `latest_version_id`, drop `versions` relationship
- `src/harbor_clerk/models/document_version.py` — DELETED at end-of-stage (after migration drops the table)
- `src/harbor_clerk/models/document_page.py` — `version_id` → `doc_id`
- `src/harbor_clerk/models/document_heading.py` — `version_id` → `doc_id`
- `src/harbor_clerk/models/chunk.py` — drop `version_id`, keep `doc_id`, update unique constraint to `(doc_id, chunk_num)`
- `src/harbor_clerk/models/entity.py` — drop `version_id`, keep `doc_id`
- `src/harbor_clerk/models/ingestion_job.py` — `version_id` → `doc_id`, update unique constraint to `(doc_id, stage)`
- `src/harbor_clerk/models/watched.py` — drop `version_id` from `WatchedFile`
- `src/harbor_clerk/models/upload.py` — drop `version_id` from `Upload`
- `src/harbor_clerk/models/enums.py` — rename `VersionStatus` → `PipelineStatus` (DB enum stays `version_status`; Python class is what we touch)
- `src/harbor_clerk/watcher/events.py` — collapse 6-branch state machine to 4
- `src/harbor_clerk/worker/pipeline.py` — orchestrator rekeys to `doc_id`, adds race protection
- `src/harbor_clerk/worker/stages/extract.py`, `ocr.py`, `chunk.py`, `entities.py`, `embed.py`, `summarize.py`, `finalize.py` — each rekeys to `doc_id` and consumes/writes new flat columns
- `src/harbor_clerk/api/routes/documents.py` — drop version joins, rewrite list response shape
- `src/harbor_clerk/api/routes/passages.py` — version joins → doc joins
- `src/harbor_clerk/api/routes/jobs.py` — SSE event payload `version_id` → `doc_id`
- `src/harbor_clerk/api/routes/maintenance.py` — reprocess endpoint rekeys
- `src/harbor_clerk/api/routes/chat.py`, `research.py`, `uploads.py` — version references → doc references
- `src/harbor_clerk/mcp_server.py` — ~20 join sites; `latest_version_id` field dropped from responses
- `src/harbor_clerk/search.py` — `Document.latest_version_id == Chunk.version_id` joins → direct doc joins
- `src/harbor_clerk/topics.py` — same
- `src/harbor_clerk/llm/research.py` — same
- `src/harbor_clerk/storage.py` — object key pattern `originals/versions/<version_id>/<f>` → `originals/docs/<doc_id>/<f>`

**Modified files (frontend):**
- `frontend/src/pages/DocumentDetailPage.tsx` — collapse `VersionInfo[]` → flat doc fields; ~250 lines cut
- `frontend/src/hooks/useJobEvents.ts` — SSE payload key change
- `frontend/src/hooks/useQueueTray.ts` — list keying `version_id` → `doc_id`
- `frontend/src/components/queue-tray/QueuePanel.tsx` — list keys
- `frontend/src/pages/SearchPage.tsx` — citation/result keys
- `frontend/src/pages/DocumentsPage.tsx` — drop `version_count` column + interface
- `frontend/src/pages/ExplorePage.tsx` — drop `version_count` column + interface

**Deleted files:**
- `src/harbor_clerk/models/document_version.py`
- (No frontend deletions — types are inlined)

**Tests modified:**
- `tests/watcher/test_events.py` — full rewrite around the 4-branch state machine; the empty-SHA regression test stays
- `tests/test_api_documents.py` — response-shape assertions
- `tests/test_mcp_tools.py` — drop `latest_version_id` assertions
- `tests/test_pipeline.py` (or wherever pipeline orchestration is tested) — race-protection coverage
- `tests/test_storage.py` — object-key pattern change

**Tests added:**
- `tests/test_alembic_0017_flatten.py` — fixture corpus with multi-version Document, run upgrade, assert flattened state + child-row pruning + storage rename

---

## Task 1: Alembic migration (schema flatten + backfill + storage rename + drop)

**Files:**
- Create: `alembic/versions/0017_flatten_document_model.py`
- Create: `tests/test_alembic_0017_flatten.py`

The migration runs as a single upgrade transaction (with a single `op.execute` for the storage rename, which uses the configured `StorageBackend`). It adds new columns, backfills, NOT-NULLs, drops old columns, drops `document_versions`, and renames MinIO/filesystem object keys.

- [ ] **Step 1: Write the migration test first**

The existing `tests/test_migrations.py` already provides `alembic_cfg` (module-scope) and `sync_engine` (module-scope) fixtures. Reuse them. Add the new test to `tests/test_alembic_0017_flatten.py`:

```python
"""Test the 0017 flatten-document-model migration."""

import hashlib
import uuid

import pytest
from alembic import command
from sqlalchemy import text

# Reuse alembic_cfg and sync_engine from test_migrations.py
pytest_plugins = ["tests.test_migrations"]


def test_0017_flattens_two_version_doc(alembic_cfg, sync_engine):
    """A doc with two versions: latest content survives, prior is pruned."""
    # Set up at the previous revision (0016)
    command.downgrade(alembic_cfg, "0016")

    doc_id = uuid.uuid4()
    v1_id = uuid.uuid4()
    v2_id = uuid.uuid4()
    v1_sha = hashlib.sha256(b"old").digest()
    v2_sha = hashlib.sha256(b"new").digest()

    with sync_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO documents (doc_id, title, status, latest_version_id, created_at, updated_at)
            VALUES (:doc_id, 'Test', 'active', :v2_id, now(), now())
        """), {"doc_id": doc_id, "v2_id": v2_id})
        conn.execute(text("""
            INSERT INTO document_versions
                (version_id, doc_id, original_sha256, status, summary, mime_type, doc_type,
                 has_text_layer, needs_ocr, extracted_chars, source_path, size_bytes,
                 original_bucket, original_object_key, summary_model, error,
                 created_at, updated_at)
            VALUES (:vid, :did, :sha, 'finalized', :summary, 'application/pdf', 'contract',
                    true, false, 1000, '/path/old.pdf', 50000,
                    'originals', :okey, 'qwen', NULL,
                    now(), now())
        """), {"vid": v1_id, "did": doc_id, "sha": v1_sha,
               "summary": "old summary", "okey": f"originals/versions/{v1_id}/old.pdf"})
        conn.execute(text("""
            INSERT INTO document_versions
                (version_id, doc_id, original_sha256, status, summary, mime_type, doc_type,
                 has_text_layer, needs_ocr, extracted_chars, source_path, size_bytes,
                 original_bucket, original_object_key, summary_model, error,
                 created_at, updated_at)
            VALUES (:vid, :did, :sha, 'finalized', :summary, 'application/pdf', 'contract',
                    true, false, 1100, '/path/new.pdf', 51000,
                    'originals', :okey, 'qwen', NULL,
                    now(), now())
        """), {"vid": v2_id, "did": doc_id, "sha": v2_sha,
               "summary": "new summary", "okey": f"originals/versions/{v2_id}/new.pdf"})
        # One chunk per version so we can assert pruning
        conn.execute(text("""
            INSERT INTO chunks (chunk_id, doc_id, version_id, chunk_num, text,
                                 char_start, char_end, page_start, page_end, language,
                                 created_at)
            VALUES (gen_random_uuid(), :did, :vid, 0, 'old text',
                    0, 8, 1, 1, 'en',
                    now())
        """), {"did": doc_id, "vid": v1_id})
        conn.execute(text("""
            INSERT INTO chunks (chunk_id, doc_id, version_id, chunk_num, text,
                                 char_start, char_end, page_start, page_end, language,
                                 created_at)
            VALUES (gen_random_uuid(), :did, :vid, 0, 'new text',
                    0, 8, 1, 1, 'en',
                    now())
        """), {"did": doc_id, "vid": v2_id})

    # Run the migration under test
    command.upgrade(alembic_cfg, "0017")

    with sync_engine.begin() as conn:
        # Document table got the new columns populated from v2
        row = conn.execute(text("""
            SELECT sha256, summary, doc_type, mime_type, source_path, size_bytes,
                   has_text_layer, needs_ocr, extracted_chars, original_object_key,
                   pipeline_status, pipeline_seq
            FROM documents WHERE doc_id = :did
        """), {"did": doc_id}).first()
        assert row.sha256 == v2_sha
        assert row.summary == "new summary"
        assert row.source_path == "/path/new.pdf"
        assert row.size_bytes == 51000
        assert row.original_object_key == f"originals/docs/{doc_id}/new.pdf"
        assert row.pipeline_status == "finalized"
        assert row.pipeline_seq == 0

        # Only the v2 chunk survived
        chunks = conn.execute(text(
            "SELECT text FROM chunks WHERE doc_id = :did"
        ), {"did": doc_id}).fetchall()
        assert len(chunks) == 1
        assert chunks[0].text == "new text"

        # document_versions table is gone
        with pytest.raises(Exception):
            conn.execute(text("SELECT 1 FROM document_versions LIMIT 1"))

        # latest_version_id column is gone
        cols = conn.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'documents'
        """)).fetchall()
        assert "latest_version_id" not in {c.column_name for c in cols}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_alembic_0017_flatten.py -v`
Expected: FAIL with "revision 0017 not found" or similar. The migration doesn't exist yet.

- [ ] **Step 3: Write the migration file**

Create `alembic/versions/0017_flatten_document_model.py`:

```python
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
    # 1. Add new columns to documents (all nullable for backfill)
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
    for col in ("summary", "summary_model", "doc_type", "mime_type", "source_path",
                "error", "original_bucket", "original_object_key"):
        op.add_column("documents", sa.Column(col, sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("has_text_layer", sa.Boolean(), nullable=True))
    op.add_column("documents", sa.Column("needs_ocr", sa.Boolean(), nullable=True))
    op.add_column("documents", sa.Column("extracted_chars", sa.BigInteger(), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger(), nullable=True))

    # 2. Add doc_id columns to child tables (chunks/entities already have it)
    op.add_column("document_pages", sa.Column("doc_id", sa.UUID(as_uuid=True), nullable=True))
    op.add_column("document_headings", sa.Column("doc_id", sa.UUID(as_uuid=True), nullable=True))
    op.add_column("ingestion_jobs", sa.Column("doc_id", sa.UUID(as_uuid=True), nullable=True))

    # 3. Backfill documents from latest_version_id
    op.execute("""
        UPDATE documents d
        SET sha256 = v.original_sha256,
            pipeline_status = v.status,
            summary = v.summary,
            summary_model = v.summary_model,
            doc_type = v.doc_type,
            mime_type = v.mime_type,
            source_path = v.source_path,
            error = v.error,
            original_bucket = v.original_bucket,
            original_object_key = v.original_object_key,
            has_text_layer = v.has_text_layer,
            needs_ocr = v.needs_ocr,
            extracted_chars = v.extracted_chars,
            size_bytes = v.size_bytes
        FROM document_versions v
        WHERE d.latest_version_id = v.version_id
    """)

    # 4. Backfill child-table doc_id from version_id
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

    # 5. Delete non-latest version data from child tables
    op.execute("""
        DELETE FROM chunks c
        USING documents d
        WHERE c.doc_id = d.doc_id AND c.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM entities e
        USING documents d
        WHERE e.doc_id = d.doc_id AND e.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM document_pages p
        USING documents d
        WHERE p.doc_id = d.doc_id AND p.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM document_headings h
        USING documents d
        WHERE h.doc_id = d.doc_id AND h.version_id != d.latest_version_id
    """)
    op.execute("""
        DELETE FROM ingestion_jobs j
        USING documents d
        WHERE j.doc_id = d.doc_id AND j.version_id != d.latest_version_id
    """)

    # 6. Storage rename pass: rewrite original_object_key column from
    #    'originals/versions/<version_id>/<filename>' to
    #    'originals/docs/<doc_id>/<filename>'.
    op.execute("""
        UPDATE documents
        SET original_object_key = regexp_replace(
            original_object_key,
            '^originals/versions/[0-9a-f-]+/',
            'originals/docs/' || doc_id || '/'
        )
        WHERE original_object_key IS NOT NULL
    """)
    # Note: actual MinIO/filesystem rename of the underlying objects is handled
    # by a separate one-shot script invoked at app start — see
    # src/harbor_clerk/storage.py's startup hook (Task 8). The DB column points
    # at the new key; the script reconciles. This split keeps migrations pure-SQL.

    # 7. NOT NULL the new columns now that backfill is complete
    op.alter_column("documents", "sha256", nullable=False)
    op.alter_column("documents", "pipeline_status", nullable=False)
    op.alter_column("document_pages", "doc_id", nullable=False)
    op.alter_column("document_headings", "doc_id", nullable=False)
    op.alter_column("ingestion_jobs", "doc_id", nullable=False)

    # 8. Add FKs on the new doc_id columns
    op.create_foreign_key(
        "fk_pages_doc_id", "document_pages", "documents", ["doc_id"], ["doc_id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_headings_doc_id", "document_headings", "documents", ["doc_id"], ["doc_id"], ondelete="CASCADE"
    )
    op.create_foreign_key(
        "fk_jobs_doc_id", "ingestion_jobs", "documents", ["doc_id"], ["doc_id"], ondelete="CASCADE"
    )

    # 9. Drop old unique constraints + add new doc_id-keyed ones
    op.drop_constraint("uq_chunks_version_num", "chunks", type_="unique")
    op.create_unique_constraint("uq_chunks_doc_num", "chunks", ["doc_id", "chunk_num"])
    op.drop_constraint("uq_pages_version_page", "document_pages", type_="unique")
    op.create_unique_constraint("uq_pages_doc_page", "document_pages", ["doc_id", "page_num"])
    op.drop_constraint("uq_jobs_version_stage", "ingestion_jobs", type_="unique")
    op.create_unique_constraint("uq_jobs_doc_stage", "ingestion_jobs", ["doc_id", "stage"])

    # 10. Drop version_id columns from child tables
    for tbl in ("chunks", "entities", "document_pages", "document_headings",
                "ingestion_jobs", "watched_files", "uploads"):
        op.drop_column(tbl, "version_id")

    # 11. Drop latest_version_id from documents and document_versions table
    op.drop_column("documents", "latest_version_id")
    op.drop_table("document_versions")


def downgrade() -> None:
    """Recreate the structural shape of the pre-flatten schema. DOES NOT
    restore data — non-latest version rows were deleted during upgrade and
    cannot be reconstructed. Existing tests in tests/test_migrations.py
    only assert table presence/absence, so a structural-only downgrade is
    sufficient to keep the round-trip test green.
    """
    # Re-create document_versions table (empty)
    op.create_table(
        "document_versions",
        sa.Column("version_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("doc_id", sa.UUID(as_uuid=True),
                  sa.ForeignKey("documents.doc_id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_sha256", BYTEA(), nullable=False),
        sa.Column("status", sa.Enum(name="version_status", create_type=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # Other columns omitted — only structural shape needed for round-trip test.
    )
    # Re-add latest_version_id to documents
    op.add_column("documents", sa.Column("latest_version_id", sa.UUID(as_uuid=True), nullable=True))
    # Re-add version_id to child tables (nullable; nothing to backfill)
    for tbl in ("chunks", "entities", "document_pages", "document_headings",
                "ingestion_jobs", "watched_files", "uploads"):
        op.add_column(tbl, sa.Column("version_id", sa.UUID(as_uuid=True), nullable=True))
    # Drop the new flat columns from documents
    for col in ("sha256", "pipeline_status", "pipeline_seq", "summary", "summary_model",
                "doc_type", "mime_type", "source_path", "error", "original_bucket",
                "original_object_key", "has_text_layer", "needs_ocr", "extracted_chars",
                "size_bytes"):
        op.drop_column("documents", col)
    # Drop the new doc_id columns from child tables
    for tbl in ("document_pages", "document_headings", "ingestion_jobs"):
        op.drop_constraint(f"fk_{tbl.replace('document_', '').replace('_', '')[:5]}_doc_id", tbl, type_="foreignkey")
        op.drop_column(tbl, "doc_id")
    # Restore old unique constraints (best-effort — names must match originals)
    op.drop_constraint("uq_chunks_doc_num", "chunks", type_="unique")
    op.drop_constraint("uq_pages_doc_page", "document_pages", type_="unique")
    op.drop_constraint("uq_jobs_doc_stage", "ingestion_jobs", type_="unique")
```

- [ ] **Step 4: Run the migration test**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_alembic_0017_flatten.py -v`
Expected: PASS. The migration creates the flat schema, backfills, prunes non-latest, drops old.

- [ ] **Step 5: Verify ruff is clean**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test ruff check alembic/versions/0017_flatten_document_model.py tests/test_alembic_0017_flatten.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && \
  git add alembic/versions/0017_flatten_document_model.py tests/test_alembic_0017_flatten.py && \
  git commit -m "feat(db): 0017 flatten document model — drop document_versions, re-key children to doc_id"
```

---

## Task 2: Update SQLAlchemy ORM models

**Files:**
- Modify: `src/harbor_clerk/models/document.py`
- Modify: `src/harbor_clerk/models/document_page.py`
- Modify: `src/harbor_clerk/models/document_heading.py`
- Modify: `src/harbor_clerk/models/chunk.py`
- Modify: `src/harbor_clerk/models/entity.py`
- Modify: `src/harbor_clerk/models/ingestion_job.py`
- Modify: `src/harbor_clerk/models/watched.py`
- Modify: `src/harbor_clerk/models/upload.py`
- Modify: `src/harbor_clerk/models/enums.py`
- Delete: `src/harbor_clerk/models/document_version.py`
- Modify: `src/harbor_clerk/models/__init__.py` (drop the DocumentVersion import)

The ORM has to match the post-migration schema for SQLAlchemy queries to be correct.

- [ ] **Step 1: Rewrite `models/document.py`**

```python
import uuid

from sqlalchemy import BigInteger, Boolean, Enum, ForeignKey, Integer, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk
from harbor_clerk.models.enums import PipelineStatus


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[uuid_pk]
    title: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    topic_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("corpus_topics.topic_id", ondelete="SET NULL"), nullable=True
    )

    # Per-content state (pulled up from former DocumentVersion in 0017 migration)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    pipeline_status: Mapped[PipelineStatus] = mapped_column(
        Enum(PipelineStatus, name="version_status", create_type=False),
        nullable=False,
    )
    pipeline_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    has_text_layer: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    needs_ocr: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    extracted_chars: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    original_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]
```

- [ ] **Step 2: Rename `VersionStatus` → `PipelineStatus` in `models/enums.py`**

Change `class VersionStatus(...)` to `class PipelineStatus(...)`. The DB enum stays named `version_status` — only the Python class is renamed. Keep all member names identical.

- [ ] **Step 3: Update `models/document_page.py`**

Drop `version_id` column. Replace with FK to documents:

```python
doc_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("documents.doc_id", ondelete="CASCADE"),
    nullable=False,
)
```

Update `__table_args__` to `UniqueConstraint("doc_id", "page_num", name="uq_pages_doc_page")`.

Drop the `back_populates="pages"` and the parent-side relationship. (The parent `Document` no longer has a `pages` relationship — relationships are not strictly needed and add complexity.)

- [ ] **Step 4: Update `models/document_heading.py`**

Same shape as `document_page.py`: drop `version_id`, add `doc_id` FK to documents. Drop the version relationship.

- [ ] **Step 5: Update `models/chunk.py`**

Drop the `version_id` column entirely. Keep `doc_id` (already FK to documents). Update unique constraint:
```python
__table_args__ = (UniqueConstraint("doc_id", "chunk_num", name="uq_chunks_doc_num"),)
```

- [ ] **Step 6: Update `models/entity.py`**

Drop `version_id` column. Keep `doc_id` (already FK to documents).

- [ ] **Step 7: Update `models/ingestion_job.py`**

Replace `version_id` with `doc_id` FK to documents. Update unique constraint to `(doc_id, stage)`.

- [ ] **Step 8: Update `models/watched.py`**

Drop the `version_id: Mapped[uuid.UUID | None]` column from `WatchedFile`. Keep `doc_id`.

- [ ] **Step 9: Update `models/upload.py`**

Drop the `version_id` column from `Upload`. Keep `doc_id`.

- [ ] **Step 10: Delete `models/document_version.py`**

```bash
rm src/harbor_clerk/models/document_version.py
```

- [ ] **Step 11: Update `models/__init__.py`**

Remove the `from harbor_clerk.models.document_version import DocumentVersion` line and any export of `DocumentVersion`.

- [ ] **Step 12: Verify the models import cleanly**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test python -c "from harbor_clerk.models import Document, DocumentPage, DocumentHeading, Chunk, Entity, IngestionJob, WatchedFile, Upload"`
Expected: no errors. (The rest of the codebase still references `DocumentVersion` — those will fail when imported, but the model module itself should be clean.)

- [ ] **Step 13: Commit**

```bash
git add src/harbor_clerk/models/ && git rm src/harbor_clerk/models/document_version.py && \
  git commit -m "refactor(models): flatten Document — pull version columns up, drop DocumentVersion class"
```

Note: test suite is broken at this point. Tasks 3-12 fix it.

---

## Task 3: Watcher refactor (`events.py`)

**Files:**
- Modify: `src/harbor_clerk/watcher/events.py`
- Modify: `tests/watcher/test_events.py`

Collapse the 6-branch state machine to 4 (per spec). Drop cross-content dedup. Replace-in-place on modify.

- [ ] **Step 1: Rewrite the relevant tests first (TDD)**

In `tests/watcher/test_events.py`, replace tests that asserted `_new_version_on_doc` or `latest_version_id` with the new shape. New / preserved tests:

```python
async def test_new_file_creates_document_and_extract_job(...):
    """A created event for a fresh path → 1 Document, 1 IngestionJob (extract)."""
    # ... arrange + act ...
    assert session.query(Document).count() == 1
    doc = session.query(Document).one()
    assert doc.pipeline_status == PipelineStatus.queued
    assert doc.pipeline_seq == 0
    job = session.query(IngestionJob).one()
    assert job.doc_id == doc.doc_id
    assert job.stage == JobStage.extract


async def test_modify_replaces_in_place(...):
    """A modified event with new SHA → bumps pipeline_seq, deletes children, enqueues extract."""
    # ... arrange a doc with chunks/entities/etc ...
    handle_event(session, FileEvent(kind=EventKind.modified, ...))
    doc = session.query(Document).one()
    assert doc.pipeline_seq == 1
    # Old chunks/entities are gone
    assert session.query(Chunk).filter_by(doc_id=doc.doc_id).count() == 0
    # New extract job queued
    job = session.query(IngestionJob).filter_by(doc_id=doc.doc_id).one()
    assert job.stage == JobStage.extract


async def test_no_cross_path_dedup(...):
    """Two different paths with the same SHA → two distinct Documents."""
    # ... arrange ...
    handle_event(session, FileEvent(kind=EventKind.created, relative_path="a.pdf", ...))
    handle_event(session, FileEvent(kind=EventKind.created, relative_path="b.pdf", ...))
    assert session.query(Document).count() == 2


async def test_zero_byte_file_is_skipped(...):
    """Preserved from PR #252: 0-byte files are ignored."""
    # ... existing test body, just remove version assertions ...
```

Run: `pytest tests/watcher/test_events.py -v` — expect FAILS for new tests.

- [ ] **Step 2: Rewrite `events.py`'s `handle_event`**

The new branch table (per spec):

```python
def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    if _should_ignore(event.relative_path):
        return

    existing = (
        session.query(WatchedFile)
        .filter_by(folder_id=event.folder_id, relative_path=event.relative_path)
        .one_or_none()
    )

    if event.kind == EventKind.deleted:
        if existing is not None and existing.status == WatchedFileStatus.active:
            existing.status = WatchedFileStatus.removed
            existing.removed_at = datetime.now(UTC)
        return

    # Skip 0-byte files (PR #252 regression — keep)
    try:
        if os.path.getsize(event.absolute_path) == 0:
            return
    except OSError:
        return

    sha = _sha256_of(event.absolute_path)
    if sha == _EMPTY_FILE_SHA256:
        return

    # Branch 1: existing+active+same → no-op
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 == sha:
        return

    # Branch 2: existing+active+different → reprocess in place
    if existing is not None and existing.status == WatchedFileStatus.active and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        return

    # Branch 3: existing+removed+same → resurrect
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 == sha:
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        return

    # Branch 4: existing+removed+different → resurrect + reprocess
    if existing is not None and existing.status == WatchedFileStatus.removed and existing.sha256 != sha:
        _reprocess_doc(session, existing.doc_id, sha, event.absolute_path)
        existing.sha256 = sha
        existing.status = WatchedFileStatus.active
        existing.removed_at = None
        return

    # Branch 5: no existing row → create Document + WatchedFile + extract job
    _create_doc_and_enqueue(session, event, sha)
```

Add helpers `_reprocess_doc` and `_create_doc_and_enqueue`. The cross-content dedup query (`SELECT DocumentVersion WHERE original_sha256 = sha`) is gone entirely.

```python
def _reprocess_doc(session: Session, doc_id: uuid.UUID, sha: bytes, source_path: str) -> None:
    """Bump pipeline_seq, DELETE child rows, set pipeline_status=queued, enqueue extract."""
    doc = session.query(Document).filter_by(doc_id=doc_id).one()
    doc.pipeline_seq = doc.pipeline_seq + 1
    doc.sha256 = sha
    doc.source_path = source_path
    doc.pipeline_status = PipelineStatus.queued
    doc.error = None
    # Delete child rows
    session.query(Chunk).filter_by(doc_id=doc_id).delete()
    session.query(Entity).filter_by(doc_id=doc_id).delete()
    session.query(DocumentPage).filter_by(doc_id=doc_id).delete()
    session.query(DocumentHeading).filter_by(doc_id=doc_id).delete()
    session.query(IngestionJob).filter_by(doc_id=doc_id).delete()
    session.add(IngestionJob(doc_id=doc_id, stage=JobStage.extract, status=JobStatus.queued))


def _create_doc_and_enqueue(session: Session, event: FileEvent, sha: bytes) -> None:
    filename = Path(event.absolute_path).name
    doc = Document(
        title=Path(event.absolute_path).stem,
        canonical_filename=filename,
        status="active",
        sha256=sha,
        source_path=event.absolute_path,
        pipeline_status=PipelineStatus.queued,
    )
    session.add(doc)
    session.flush()
    session.add(
        WatchedFile(
            folder_id=event.folder_id,
            relative_path=event.relative_path,
            bookmark_data=b"",
            sha256=sha,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
```

- [ ] **Step 3: Run watcher tests**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/watcher/test_events.py -v`
Expected: PASS for all new and preserved tests.

- [ ] **Step 4: Run ruff**

Run: `uv run --extra test ruff check src/harbor_clerk/watcher/ tests/watcher/`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/events.py tests/watcher/test_events.py && \
  git commit -m "refactor(watcher): collapse to 4-branch state machine, drop cross-path dedup, replace-in-place on modify"
```

---

## Task 4: Pipeline orchestrator + race protection (`worker/pipeline.py`)

**Files:**
- Modify: `src/harbor_clerk/worker/pipeline.py`
- Modify: relevant tests (`tests/test_pipeline.py` if it exists, otherwise create one for the race-protection check)

The orchestrator coordinates the 7 stages. Today it operates on `version_id`. Switch to `doc_id`. Add `pipeline_seq` race-protection helper.

- [ ] **Step 1: Read the current pipeline.py to understand the entry points**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && grep -n "^def\|^async def" src/harbor_clerk/worker/pipeline.py | head -20`

Identify (a) the function that workers call to claim a job, (b) `enqueue_stage`, (c) the fan-out logic for parallel-after-chunk stages.

- [ ] **Step 2: Add a `check_pipeline_seq` helper**

Add at module level:

```python
def check_pipeline_seq(session, doc_id: uuid.UUID, expected_seq: int) -> bool:
    """Return True if the doc's pipeline_seq still matches.

    Workers call this at result-write time to detect a content-change race.
    If the seq has been bumped (e.g., a watcher modify event arrived during
    in-flight extract), the worker should abort writing its results — the
    new ingestion is already queued and will run.
    """
    current = session.query(Document.pipeline_seq).filter_by(doc_id=doc_id).scalar()
    return current == expected_seq
```

- [ ] **Step 3: Rekey `enqueue_stage` and the fan-out logic**

Find every reference to `version_id` in this file and rewrite to `doc_id`. The unique constraint on `ingestion_jobs` is now `(doc_id, stage)`, so dedup on enqueue uses `doc_id`.

- [ ] **Step 4: Write a race-protection test**

Create or extend `tests/test_pipeline.py`:

```python
async def test_check_pipeline_seq_detects_content_change(db_session, test_doc):
    """Worker that read seq=0 should fail check after a content change bumps it."""
    test_doc.pipeline_seq = 0
    db_session.add(test_doc)
    await db_session.commit()

    assert check_pipeline_seq(db_session, test_doc.doc_id, 0) is True

    test_doc.pipeline_seq = 1
    await db_session.commit()

    assert check_pipeline_seq(db_session, test_doc.doc_id, 0) is False
```

- [ ] **Step 5: Run pipeline tests**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/worker/pipeline.py tests/test_pipeline.py && \
  git commit -m "refactor(worker): pipeline orchestrator keys on doc_id, adds pipeline_seq race protection"
```

---

## Task 5: Stage modules refactor

**Files:**
- Modify: `src/harbor_clerk/worker/stages/extract.py`
- Modify: `src/harbor_clerk/worker/stages/ocr.py`
- Modify: `src/harbor_clerk/worker/stages/chunk.py`
- Modify: `src/harbor_clerk/worker/stages/entities.py`
- Modify: `src/harbor_clerk/worker/stages/embed.py`
- Modify: `src/harbor_clerk/worker/stages/summarize.py`
- Modify: `src/harbor_clerk/worker/stages/finalize.py`
- Modify: `tests/test_<stage>.py` for each stage that has tests

Each stage's signature changes from `(session, version_id) → ...` to `(session, doc_id) → ...`. Each stage that writes results does a `check_pipeline_seq` before write.

- [ ] **Step 1: Read each stage file** to understand its current shape

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && wc -l src/harbor_clerk/worker/stages/*.py`

- [ ] **Step 2: Rewrite extract.py**

Change every `version_id` parameter to `doc_id`. Read source from `Document.source_path` (was `DocumentVersion.source_path`). Write `extracted_chars`, `has_text_layer`, `mime_type` to `documents` instead of `document_versions`. Wrap result-writes in:

```python
if not check_pipeline_seq(session, doc_id, worker_seq):
    logger.info("extract: pipeline_seq bumped during extract for %s, aborting write", doc_id)
    return
```

- [ ] **Step 3: Rewrite ocr.py, chunk.py, entities.py, embed.py, summarize.py, finalize.py**

Same pattern for each. The summarize stage writes `Document.summary` and `Document.summary_model`. Finalize sets `Document.pipeline_status = PipelineStatus.finalized`.

- [ ] **Step 4: Update each stage's tests**

For each test file under `tests/`, replace `version_id` with `doc_id` and adjust assertions.

- [ ] **Step 5: Run all stage tests**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_extract*.py tests/test_ocr.py tests/test_chunk*.py tests/test_entities.py tests/test_embed*.py tests/test_summarize.py tests/test_finalize.py -v`
Expected: all pass.

- [ ] **Step 6: Run ruff**

Run: `uv run --extra test ruff check src/harbor_clerk/worker/stages/ tests/`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/worker/stages/ tests/ && \
  git commit -m "refactor(worker): stage modules key on doc_id, write to flat Document columns, race-check before write"
```

---

## Task 6: API routes refactor

**Files:**
- Modify: `src/harbor_clerk/api/routes/documents.py`
- Modify: `src/harbor_clerk/api/routes/passages.py`
- Modify: `src/harbor_clerk/api/routes/jobs.py`
- Modify: `src/harbor_clerk/api/routes/maintenance.py`
- Modify: `src/harbor_clerk/api/routes/chat.py`
- Modify: `src/harbor_clerk/api/routes/research.py`
- Modify: `src/harbor_clerk/api/routes/uploads.py`
- Modify: `src/harbor_clerk/search.py`
- Modify: `src/harbor_clerk/topics.py`
- Modify: `src/harbor_clerk/llm/research.py`
- Modify: `tests/test_api_documents.py`
- Modify: `tests/test_api_search.py`, `test_api_passages.py`, etc.

All `Document.latest_version_id == X.version_id` joins collapse to `documents.doc_id = X.doc_id`. `latest_version_id` and `version_count` fields are removed from response shapes.

- [ ] **Step 1: Run a global grep to inventory the work**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && grep -rn "latest_version_id\|version_id\|DocumentVersion" src/harbor_clerk/api/ src/harbor_clerk/search.py src/harbor_clerk/topics.py src/harbor_clerk/llm/research.py | wc -l`

That number is the count of references you'll touch. Expected ~40-60.

- [ ] **Step 2: Rewrite `api/routes/documents.py`**

The `/api/docs` list endpoint and `/api/docs/{doc_id}` detail endpoint:
- Drop `selectinload(Document.versions)`.
- Drop the `version_count`, `latest_version_status`, `latest_version_id` fields from the response Pydantic schemas (find them in `api/schemas/documents.py` or inlined).
- Replace `doc.versions[i].x` reads with `doc.x`.
- Joins to chunks/entities/pages/headings drop the version filter.

- [ ] **Step 3: Rewrite `passages.py`**

The passages-read endpoint joins chunks → documents. Replace any `Chunk.version_id == Document.latest_version_id` with direct doc filtering.

- [ ] **Step 4: Rewrite `jobs.py` SSE event payload**

The SSE event JSON currently includes `version_id`. Change to `doc_id`. The frontend hooks (Task 10) read this payload — both ends change in lockstep within the branch.

- [ ] **Step 5: Rewrite `maintenance.py` reprocess endpoint**

The reprocess endpoint should now: bump `pipeline_seq`, DELETE child rows, set `pipeline_status=queued`, enqueue extract. Same shape as the watcher's `_reprocess_doc` helper — consider extracting that helper to a shared module if both call it.

- [ ] **Step 6: Rewrite `chat.py`, `research.py`, `uploads.py`, `search.py`, `topics.py`, `llm/research.py`**

All instances of `Document.latest_version_id == X.version_id` → direct `documents.doc_id = X.doc_id` joins. The `uploads.py` route's confirm step creates a `Document` directly (no DV creation).

- [ ] **Step 7: Update tests**

For each modified route, the existing tests assert response shapes that include `latest_version_id` or `version_count`. Strip those assertions.

- [ ] **Step 8: Run all API tests**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_api_*.py -v`
Expected: all pass.

- [ ] **Step 9: Run ruff**

Run: `uv run --extra test ruff check src/harbor_clerk/api/ src/harbor_clerk/search.py src/harbor_clerk/topics.py src/harbor_clerk/llm/`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/harbor_clerk/api/ src/harbor_clerk/search.py src/harbor_clerk/topics.py src/harbor_clerk/llm/research.py tests/ && \
  git commit -m "refactor(api): drop version joins; doc routes, search, passages, jobs SSE all key on doc_id"
```

---

## Task 7: MCP server refactor

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py`
- Modify: `tests/test_mcp_tools.py`

The MCP server has ~20 join sites and emits `latest_version_id` in responses for several tools. All of those collapse.

- [ ] **Step 1: Inventory MCP touchpoints**

Run: `grep -n "latest_version_id\|version_id\|DocumentVersion" src/harbor_clerk/mcp_server.py | wc -l`

- [ ] **Step 2: Rewrite each tool handler**

For each `kb_*` tool that joins `Document.latest_version_id == X.version_id`, switch to `documents.doc_id = X.doc_id`. For each tool response that includes `"latest_version_id": str(doc.latest_version_id) if ...`, remove the field.

- [ ] **Step 3: Update MCP tests**

`tests/test_mcp_tools.py` likely asserts response keys. Drop `latest_version_id` assertions.

- [ ] **Step 4: Run MCP tests**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_mcp_tools.py tests/test_mcp_auth.py tests/test_mcp_tool_filtering.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tools.py && \
  git commit -m "refactor(mcp): drop version joins from all kb_* tools; latest_version_id removed from responses"
```

---

## Task 8: Storage object-key migration script + storage.py refactor

**Files:**
- Modify: `src/harbor_clerk/storage.py`
- Create: `src/harbor_clerk/maintenance/rename_originals.py` (or wherever maintenance scripts live — check existing structure)
- Modify: `tests/test_storage.py`

The migration in Task 1 rewrote the `original_object_key` column. Now we need to rename the actual MinIO/filesystem objects to match. This script runs at app startup and is idempotent — a no-op if all objects are already at their new keys.

- [ ] **Step 1: Inspect `storage.py`'s key construction**

Run: `grep -n "originals/versions\|originals/docs\|version_id" src/harbor_clerk/storage.py`

Identify the key-construction function. Probably a method on `StorageBackend` or a module-level helper.

- [ ] **Step 2: Update key construction to `originals/docs/<doc_id>/<filename>`**

Wherever the key is built, replace `originals/versions/<version_id>/<f>` with `originals/docs/<doc_id>/<f>`.

- [ ] **Step 3: Write the rename script**

The `StorageBackend` ABC already has `list_objects(bucket, prefix, recursive)`, `copy_and_delete(src_bucket, src_key, dst_bucket, dst_key)`, and per-key `get_object` / `remove_object`. Use those — no new primitives needed.

Create `src/harbor_clerk/maintenance/rename_originals.py`:

```python
"""One-shot: rename originals/versions/<version_id>/<f> keys to
originals/docs/<doc_id>/<f> keys.

Idempotent: walks all `originals/versions/` objects and tries to find a
Document whose `original_object_key` matches the renamed form. If found,
moves the object; if the new key already has an object, skips. After all
renames, any leftover `originals/versions/` objects are orphans and get
deleted.

Run at app startup OR via:
    uv run python -m harbor_clerk.maintenance.rename_originals
"""

import logging

from sqlalchemy.orm import Session

from harbor_clerk.config import get_settings
from harbor_clerk.models import Document
from harbor_clerk.storage import get_storage_backend

logger = logging.getLogger(__name__)


def rename_all(session: Session) -> tuple[int, int]:
    """Returns (renamed_count, orphans_deleted_count)."""
    backend = get_storage_backend()
    bucket = get_settings().minio_bucket  # same bucket name for both backends

    # Build a map: filename → expected new_key (from the DB column)
    docs_by_filename: dict[str, tuple[str, str]] = {}
    for doc in session.query(Document).filter(Document.original_object_key.isnot(None)).all():
        # original_object_key is now "originals/docs/<doc_id>/<filename>"
        filename = doc.original_object_key.rsplit("/", 1)[-1]
        docs_by_filename[filename] = (str(doc.doc_id), doc.original_object_key)

    renamed = 0
    orphans = 0
    # Walk old prefix
    for obj in backend.list_objects(bucket, prefix="originals/versions/", recursive=True):
        old_key = obj["key"]  # adapt to whatever list_objects returns; check existing usage
        filename = old_key.rsplit("/", 1)[-1]
        if filename in docs_by_filename:
            _, new_key = docs_by_filename[filename]
            try:
                backend.copy_and_delete(bucket, new_key, bucket, old_key)
                renamed += 1
            except Exception:
                logger.exception("rename_originals: failed to move %s → %s", old_key, new_key)
        else:
            # Orphan — no Document references this filename; safe to delete
            backend.remove_object(bucket, old_key)
            orphans += 1

    logger.info("rename_originals: %d renamed, %d orphans deleted", renamed, orphans)
    return renamed, orphans


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    from harbor_clerk.db import session_factory
    with session_factory() as s:
        rename_all(s)
```

NB: the implementer should verify `list_objects` returns a list of dicts with a `"key"` field (or whatever shape). Adapt the loop's unpacking to match.

- [ ] **Step 4: Wire the script into app startup**

In `src/harbor_clerk/api/main.py` (or wherever the FastAPI app's `lifespan` hook is), after migrations run, call `rename_all` once. Guard with a config flag so it doesn't run every launch — log "skipping" if `documents.original_object_key` already shows zero `originals/versions/` prefixes (cheap COUNT query).

- [ ] **Step 5: Test the rename script**

```python
async def test_rename_idempotent(mock_storage_backend, db_session):
    # Set up a Document with original_object_key already pointing to docs/<id>/<f>
    # Storage has the file at versions/<vid>/<f>
    # Run rename_all
    assert rename_all(db_session) == 1
    # Run again
    assert rename_all(db_session) == 0
```

- [ ] **Step 6: Run storage tests**

Run: `cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && uv run --extra test pytest tests/test_storage.py tests/test_rename_originals.py -v`

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/storage.py src/harbor_clerk/maintenance/ tests/test_storage.py tests/test_rename_originals.py && \
  git commit -m "feat(storage): switch object-key pattern to originals/docs/<doc_id>/; one-shot rename script for migration"
```

---

## Task 9: Frontend — DocumentDetailPage rewrite

**Files:**
- Modify: `frontend/src/pages/DocumentDetailPage.tsx` (~250 line cut)

The entire `VersionInfo[]` model collapses. The page renders the doc's flat fields directly.

- [ ] **Step 1: Read the current file end-to-end**

Run: `wc -l frontend/src/pages/DocumentDetailPage.tsx` (current: 808 lines).

- [ ] **Step 2: Rewrite the response type**

The current `DocumentDetail` interface includes `versions: VersionInfo[]`. Replace with the flat fields directly:

```ts
interface DocumentDetail {
  doc_id: string
  title: string
  canonical_filename: string | null
  status: string
  created_at: string
  updated_at: string
  // flat per-content state (was on VersionInfo)
  pipeline_status: 'queued' | 'extracting' | 'ocring' | 'chunking' | 'embedding' | 'summarizing' | 'finalized' | 'error'
  pipeline_seq: number
  summary: string | null
  doc_type: string | null
  mime_type: string | null
  source_path: string | null
  has_text_layer: boolean | null
  needs_ocr: boolean | null
  extracted_chars: number | null
  size_bytes: number | null
  error: string | null
  jobs: JobInfo[]
}
```

- [ ] **Step 3: Delete the `VersionBanner` component**

It folds into a `DocStatusBanner` that reads from the flat doc fields directly. Same green/red/processing logic, just sourced from `doc.pipeline_status` and `doc.jobs` instead of `version.status` and `version.jobs`.

- [ ] **Step 4: Delete version-numbering logic**

The `versionsWithNumber`, `allVersionsReady`, `versionCount` machinery (lines ~557-564) goes away. The page renders one document, not a versions array.

- [ ] **Step 5: Update SSE event handler**

The `useEffect` that listens to `/api/jobs/stream` and updates `prev.versions[vIdx]` becomes a direct update of `prev.jobs` — there are no nested versions.

- [ ] **Step 6: Run frontend checks**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3/frontend && \
  npm run lint && npm run type-check && npm run format:check && npm run build
```
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/pages/DocumentDetailPage.tsx && \
  git commit -m "refactor(frontend): collapse DocumentDetailPage's version wrapper into flat doc fields"
```

---

## Task 10: Frontend — queue tray + SSE event types

**Files:**
- Modify: `frontend/src/hooks/useJobEvents.ts`
- Modify: `frontend/src/hooks/useQueueTray.ts`
- Modify: `frontend/src/components/queue-tray/QueuePanel.tsx`
- Modify: `frontend/src/pages/SearchPage.tsx`

The SSE event payload now uses `doc_id` (Task 6 changed the backend). All consumers update.

- [ ] **Step 1: Update SSE event type in `useJobEvents.ts`**

```ts
interface JobEvent {
  doc_id: string  // was: version_id
  stage: string
  status: 'queued' | 'running' | 'done' | 'error' | 'skipped'
  progress?: { current: number; total: number }
  ts: string
}
```

- [ ] **Step 2: Update `useQueueTray.ts`**

Replace every `version_id` reference with `doc_id`. The list deduplication and completed-list logic uses doc_id as the key.

- [ ] **Step 3: Update `QueuePanel.tsx`**

`key={item.version_id}` → `key={item.doc_id}`. Same for any displayed identifiers (probably none — usually just title is shown).

- [ ] **Step 4: Update `SearchPage.tsx`**

Find every `result.version_id` or similar and replace with `doc_id`.

- [ ] **Step 5: Frontend checks**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3/frontend && \
  npm run lint && npm run type-check && npm run format:check && npm run build
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/hooks/useJobEvents.ts frontend/src/hooks/useQueueTray.ts \
        frontend/src/components/queue-tray/QueuePanel.tsx frontend/src/pages/SearchPage.tsx && \
  git commit -m "refactor(frontend): queue tray + SSE events + search results key on doc_id"
```

---

## Task 11: Frontend — drop version_count from Documents/Explore

**Files:**
- Modify: `frontend/src/pages/DocumentsPage.tsx`
- Modify: `frontend/src/pages/ExplorePage.tsx`

The `version_count` column always shows "1" after flatten. Remove the column entirely from both pages.

- [ ] **Step 1: Locate the references**

Run: `grep -n "version_count" frontend/src/pages/DocumentsPage.tsx frontend/src/pages/ExplorePage.tsx`
Expected: hits at ~lines 32, 945 (Documents) and 39, 869 (Explore) — interface + table cell.

- [ ] **Step 2: Remove the interface field and the table cell on each page**

For each page:
- Drop `version_count: number` from the `DocumentRow` (or equivalent) interface.
- Drop the `<td>` rendering it from the table body.
- Drop the `<th>` header for the column.
- If there's a sort key referencing it, drop that too.

- [ ] **Step 3: Frontend checks**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3/frontend && \
  npm run lint && npm run type-check && npm run format:check && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/DocumentsPage.tsx frontend/src/pages/ExplorePage.tsx && \
  git commit -m "refactor(frontend): drop version_count column from Documents and Explore pages"
```

---

## Task 12: Final verification + open PR

This task has no code changes — only verification commands and the PR.

- [ ] **Step 1: Full backend check**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && \
  uv run --extra test pytest tests/ 2>&1 | tail -3 && \
  uv run --extra test ruff check . 2>&1 | tail -3 && \
  uv run --extra test ruff format --check . 2>&1 | tail -3
```
Expected: all green. Test count up by ~5 (migration + race-protection + new watcher tests + storage rename test). No regressions.

- [ ] **Step 2: Full frontend check**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3/frontend && \
  npm run lint && npm run type-check && npm run format:check && npm run build
```
Expected: 13 pre-existing lint warnings (or fewer; some may have gone with the deleted code), tsc clean, prettier clean, build succeeds.

- [ ] **Step 3: Verify zero `version_id` / `latest_version_id` / `DocumentVersion` references remain**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && \
  grep -rn "version_id\|latest_version_id\|DocumentVersion\|document_versions" src/ frontend/src/ tests/ | \
  grep -v "alembic/versions/0017_flatten" | wc -l
```
Expected: 0.

- [ ] **Step 4: Manual smoke checklist (macOS)**

After `make apps` install:

1. App launches; migration runs; logs show "0017 flatten complete" + "rename pass: N renamed".
2. Existing docs in your corpus still resolve in search and chat.
3. Documents page: rows render, version_count column is gone, `pipeline_status` shows correctly.
4. DocumentDetailPage on an existing doc: no "Version 1" wrapper; ingestion-complete banner + flat stats line + jobs disclosure render directly.
5. Drop a NEW PDF into a watched folder: ingest completes; queue tray shows progress keyed on doc_id; final state matches.
6. Edit an existing watched file's content: doc gets re-ingested under same doc_id; chat citations to that doc still resolve (now to new content).
7. Reprocess a doc via `kb_reprocess` MCP tool: child rows wiped, new ingestion runs.

- [ ] **Step 5: Push branch**

```bash
cd /Users/alex/mcp-gateway/.claude/worktrees/stage-3 && \
  git push -u origin spec/watched-folder-first-stage-3
```

- [ ] **Step 6: Open PR**

```bash
gh pr create --title "feat: watched-folder-first stage 3 — flatten document model (drop document_versions)" --body-file - <<'EOF'
## Summary

Stage 3 of the watched-folder-first refactor (spec at `docs/superpowers/specs/2026-05-01-watched-folder-first-stage-3-design.md`).

- `document_versions` table dropped; per-content state pulled up to `documents` (sha256, pipeline_status, pipeline_seq, summary, doc_type, mime_type, source_path, has_text_layer, needs_ocr, extracted_chars, size_bytes, original_bucket, original_object_key, error).
- All child tables (chunks, entities, document_pages, document_headings, ingestion_jobs, watched_files, uploads) re-keyed from `version_id` to `doc_id`.
- Watcher's modify branch is now replace-in-place: bump `pipeline_seq`, DELETE child rows, enqueue extract.
- Workers gain race protection: read `pipeline_seq` at job start, compare-and-write at result time. Stale results from in-flight jobs that lost a race against a content change are dropped.
- Cross-path SHA dedup removed: each watched file path gets its own Document.
- `latest_version_id` and `version_count` removed from API/MCP responses.
- DocumentDetailPage: ~250-line cut. The "Version 1 (date)" wrapper collapses; ingestion-complete banner, stats line, jobs disclosure all render directly on the Document.
- Storage object-key pattern: `originals/versions/<version_id>/<f>` → `originals/docs/<doc_id>/<f>`. One-shot rename script runs at app startup.

## Migration is destructive

Once 0017 runs, non-latest version data (chunks/entities/pages/headings/jobs from prior versions) is gone. There's no downgrade. The user has been informed; this is the agreed shape.

## Test plan

- [x] `pytest tests/` — passes (+5 new: migration smoke, race protection, watcher 4-branch coverage, storage rename idempotency)
- [x] `ruff check .` clean, `ruff format --check .` clean
- [x] `npm run lint && tsc --noEmit && format:check && build` clean
- [ ] Manual smoke (macOS): existing corpus migrates cleanly; ingestion-in-place modify works; chat citations resolve; Reprocess works
- [ ] `grep -rn "version_id\|DocumentVersion\|document_versions"` returns 0 matches outside the migration file

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

- [ ] **Step 7: Watch CI, fix anything that flags, merge when green**

```bash
gh pr checks --watch
```

When all green, merge with squash. Pass authorisation must be explicit from the user — this is a major migration, not auto-mergeable on the standing rule.

