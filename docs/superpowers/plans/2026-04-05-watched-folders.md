# Watched Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let macOS users point Harbor Clerk at filesystem directories and have files automatically ingested, tracked for changes, and cleaned up on deletion — without copying originals.

**Architecture:** Swift FSEvents watcher detects file changes, resolves identity via macOS bookmarks, and calls Python API endpoints that create documents/versions referencing files in place. Priority-aware job queue ensures GUI uploads preempt watched folder ingestion. 30-day soft-delete reaper handles missing files.

**Tech Stack:** Swift (FSEvents, UTType, URL bookmarks), Python (FastAPI, SQLAlchemy, Alembic), PostgreSQL, React/TypeScript

**Spec:** `docs/superpowers/specs/2026-04-05-watched-folders-design.md`

---

## File Map

### New Files

| File | Responsibility |
|---|---|
| `alembic/versions/0011_watched_folders.py` | Migration: new tables, schema changes |
| `src/harbor_clerk/models/watched.py` | SQLAlchemy models: WatchedFolder, WatchedFile |
| `src/harbor_clerk/api/routes/watch.py` | API endpoints for watched folder CRUD + ingest/remove/rename |
| `tests/test_watch_api.py` | API endpoint tests |
| `tests/test_watch_pipeline.py` | Pipeline integration tests (source_path, priority) |
| `macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift` | FSEvents watcher, bookmark management, API client |

### Modified Files

| File | Changes |
|---|---|
| `src/harbor_clerk/models/__init__.py` | Export WatchedFolder, WatchedFile |
| `src/harbor_clerk/models/document_version.py` | Make original_bucket/original_object_key nullable, drop SHA256 unique |
| `src/harbor_clerk/models/ingestion_job.py` | Add priority column |
| `src/harbor_clerk/worker/pipeline.py` | Priority param on enqueue_stage, propagation in advance_pipeline, _version_filename fallback |
| `src/harbor_clerk/worker/entry.py` | ORDER BY priority ASC in claim_next_job |
| `src/harbor_clerk/worker/stages/extract.py` | source_path file reading fallback |
| `src/harbor_clerk/worker/stages/ocr.py` | source_path file reading fallback |
| `src/harbor_clerk/api/routes/documents.py` | Download from source_path, watch_source_path/watch_status in list response |
| `src/harbor_clerk/api/app.py` | Reaper addition for 30-day watched file cleanup |
| `src/harbor_clerk/api/routes/uploads.py` | Export ALLOWED_EXTENSIONS (or move to shared location) |
| `frontend/src/pages/UploadPage.tsx` | macOS-only watched folders hint |
| `frontend/src/pages/DocumentsPage.tsx` | Watch indicator icon + tooltip, removed status styling |
| `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift` | Watched Folders settings section |
| `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` | Start/stop WatchedFolderManager |
| `macos/HarborClerkServer/HarborClerkServer/Settings.swift` | No changes needed (state lives in DB) |
| `README.md` | Document watched folders feature |
| `CLAUDE.md` | Update architecture notes |

---

## Task 1: Alembic Migration

**Files:**
- Create: `alembic/versions/0011_watched_folders.py`
- Modify: `src/harbor_clerk/models/document_version.py:20-26`
- Modify: `src/harbor_clerk/models/ingestion_job.py`

- [ ] **Step 1: Write the migration**

Create `alembic/versions/0011_watched_folders.py`. The revision depends on `0010_research_depth`. Use idempotent guards (check column/table exists before altering) consistent with existing migrations.

```python
"""Watched folders support.

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0011"
down_revision = "0010"

def upgrade() -> None:
    # --- New tables ---
    op.create_table(
        "watched_folders",
        sa.Column("folder_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("bookmark_data", sa.LargeBinary(), nullable=False),
        sa.Column("recursive", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_event_id", sa.BigInteger(), nullable=True),
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'watched_file_status') THEN
                CREATE TYPE watched_file_status AS ENUM ('active', 'removed');
            END IF;
        END $$;
    """)

    op.create_table(
        "watched_files",
        sa.Column("file_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("folder_id", UUID(as_uuid=True),
                  sa.ForeignKey("watched_folders.folder_id", ondelete="CASCADE"), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("bookmark_data", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column("doc_id", UUID(as_uuid=True),
                  sa.ForeignKey("documents.doc_id", ondelete="SET NULL"), nullable=True),
        sa.Column("version_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Enum("active", "removed", name="watched_file_status", create_type=False),
                  nullable=False, server_default="active"),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("folder_id", "relative_path", name="uq_watched_files_folder_path"),
    )
    op.create_index("ix_watched_files_folder_id", "watched_files", ["folder_id"])
    op.create_index("ix_watched_files_status", "watched_files", ["status"])

    # --- Modify ingestion_jobs: add priority ---
    op.add_column("ingestion_jobs",
                  sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"))
    op.create_index("ix_ingestion_jobs_priority_created",
                    "ingestion_jobs", ["priority", "created_at"])

    # --- Modify document_versions ---
    # Drop unique constraint on original_sha256 (idempotent — check first)
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'document_versions_original_sha256_key'
            ) THEN
                ALTER TABLE document_versions
                DROP CONSTRAINT document_versions_original_sha256_key;
            END IF;
        END $$;
    """)
    # Make original_bucket and original_object_key nullable
    op.alter_column("document_versions", "original_bucket", nullable=True)
    op.alter_column("document_versions", "original_object_key", nullable=True)


def downgrade() -> None:
    op.alter_column("document_versions", "original_object_key", nullable=False)
    op.alter_column("document_versions", "original_bucket", nullable=False)
    op.create_unique_constraint("document_versions_original_sha256_key",
                                "document_versions", ["original_sha256"])
    op.drop_index("ix_ingestion_jobs_priority_created", "ingestion_jobs")
    op.drop_column("ingestion_jobs", "priority")
    op.drop_table("watched_files")
    op.drop_table("watched_folders")
    op.execute("DROP TYPE IF EXISTS watched_file_status")
```

- [ ] **Step 2: Update DocumentVersion model**

In `src/harbor_clerk/models/document_version.py`, lines 20-26:

```python
# Before:
original_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, unique=True)
original_bucket: Mapped[str] = mapped_column(Text, nullable=False)
original_object_key: Mapped[str] = mapped_column(Text, nullable=False)

# After:
original_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
original_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
original_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
```

- [ ] **Step 3: Update IngestionJob model**

In `src/harbor_clerk/models/ingestion_job.py`, add after the existing columns:

```python
priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
```

Import `SmallInteger` from sqlalchemy.

- [ ] **Step 4: Run migration and verify**

```bash
cd /Users/alex/mcp-gateway
# Check current DB state (if running locally, use the macOS app's postgres)
uv run alembic upgrade head
```

Verify: no errors, tables created, columns altered.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check alembic/versions/0011_watched_folders.py src/harbor_clerk/models/document_version.py src/harbor_clerk/models/ingestion_job.py
uv run ruff format alembic/versions/0011_watched_folders.py src/harbor_clerk/models/document_version.py src/harbor_clerk/models/ingestion_job.py
git add alembic/versions/0011_watched_folders.py src/harbor_clerk/models/document_version.py src/harbor_clerk/models/ingestion_job.py
git commit -m "feat: migration for watched folders tables and schema changes"
```

---

## Task 2: SQLAlchemy Models for WatchedFolder and WatchedFile

**Files:**
- Create: `src/harbor_clerk/models/watched.py`
- Modify: `src/harbor_clerk/models/__init__.py`

- [ ] **Step 1: Create the models**

Create `src/harbor_clerk/models/watched.py`:

```python
import uuid
from enum import Enum as PyEnum

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, LargeBinary, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, updated_at, uuid_pk


class WatchedFileStatus(PyEnum):
    active = "active"
    removed = "removed"


class WatchedFolder(Base):
    __tablename__ = "watched_folders"

    folder_id: Mapped[uuid_pk]
    path: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]

    files: Mapped[list["WatchedFile"]] = relationship(back_populates="folder", cascade="all, delete-orphan")


class WatchedFile(Base):
    __tablename__ = "watched_files"

    file_id: Mapped[uuid_pk]
    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("watched_folders.folder_id", ondelete="CASCADE"),
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[WatchedFileStatus] = mapped_column(
        Enum(WatchedFileStatus, name="watched_file_status", create_type=False),
        nullable=False,
        server_default="active",
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]
    updated_at: Mapped[updated_at]

    folder: Mapped["WatchedFolder"] = relationship(back_populates="files")
```

- [ ] **Step 2: Export from models __init__**

In `src/harbor_clerk/models/__init__.py`, add the import alongside existing model imports:

```python
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
```

And add to `__all__` if one exists.

- [ ] **Step 3: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/models/watched.py src/harbor_clerk/models/__init__.py
uv run ruff format src/harbor_clerk/models/watched.py src/harbor_clerk/models/__init__.py
git add src/harbor_clerk/models/watched.py src/harbor_clerk/models/__init__.py
git commit -m "feat: SQLAlchemy models for WatchedFolder and WatchedFile"
```

---

## Task 3: Pipeline Changes — Priority and source_path

**Files:**
- Modify: `src/harbor_clerk/worker/pipeline.py:75-127` (enqueue_stage), `186-260` (advance_pipeline), `31-32` (_version_filename)
- Modify: `src/harbor_clerk/worker/entry.py:103-112` (claim_next_job ordering)
- Modify: `src/harbor_clerk/worker/stages/extract.py:171-173` (file reading)
- Modify: `src/harbor_clerk/worker/stages/ocr.py:69-71` (file reading)
- Create: `tests/test_watch_pipeline.py`

- [ ] **Step 1: Write failing tests for priority ordering and source_path**

Create `tests/test_watch_pipeline.py`:

```python
"""Tests for watched folder pipeline integration."""
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_version_filename_with_source_path():
    """_version_filename falls back to source_path when original_object_key is None."""
    from harbor_clerk.worker.pipeline import _version_filename

    version = MagicMock()
    version.original_object_key = None
    version.source_path = "/Users/test/Documents/report.pdf"
    assert _version_filename(version) == "report.pdf"


def test_version_filename_with_object_key():
    """_version_filename prefers original_object_key when present."""
    from harbor_clerk.worker.pipeline import _version_filename

    version = MagicMock()
    version.original_object_key = "versions/abc/report.pdf"
    version.source_path = "/Users/test/Documents/report.pdf"
    assert _version_filename(version) == "report.pdf"


def test_version_filename_neither():
    """_version_filename returns 'unknown' when both are None/empty."""
    from harbor_clerk.worker.pipeline import _version_filename

    version = MagicMock()
    version.original_object_key = None
    version.source_path = None
    assert _version_filename(version) == "unknown"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/alex/mcp-gateway
uv run pytest tests/test_watch_pipeline.py -v
```

Expected: FAIL — `_version_filename` doesn't handle None `original_object_key`.

- [ ] **Step 3: Update `_version_filename` in pipeline.py**

In `src/harbor_clerk/worker/pipeline.py`, around line 31:

```python
# Before:
def _version_filename(version: DocumentVersion) -> str:
    return posixpath.basename(version.original_object_key)

# After:
def _version_filename(version: DocumentVersion) -> str:
    if version.original_object_key:
        return posixpath.basename(version.original_object_key)
    if version.source_path:
        return posixpath.basename(version.source_path)
    return "unknown"
```

- [ ] **Step 4: Add priority parameter to `enqueue_stage`**

In `src/harbor_clerk/worker/pipeline.py`, update `enqueue_stage` signature (around line 75):

```python
# Before:
def enqueue_stage(version_id: uuid.UUID, stage: JobStage, ...) -> None:

# After:
def enqueue_stage(version_id: uuid.UUID, stage: JobStage, ..., priority: int = 0) -> None:
```

When creating the `IngestionJob` (around line 101), pass `priority=priority`:

```python
job = IngestionJob(
    version_id=version_id,
    stage=stage,
    status=JobStatus.queued,
    priority=priority,
)
```

- [ ] **Step 5: Propagate priority in `advance_pipeline`**

In `src/harbor_clerk/worker/pipeline.py`, update `advance_pipeline` (around line 186). It needs to read the priority from the completed job and pass it through:

```python
# At the top of advance_pipeline, after loading the version:
# Get priority from the completed job to propagate to downstream stages
completed_job = session.execute(
    select(IngestionJob)
    .where(IngestionJob.version_id == version_id, IngestionJob.stage == completed_stage)
).scalar_one_or_none()
priority = completed_job.priority if completed_job else 0
```

Then pass `priority=priority` to every `enqueue_stage()` call within `advance_pipeline`.

**Also update `_mark_skipped()`**: this helper creates `IngestionJob` rows for skipped stages (e.g., OCR when not needed, entities when spaCy unavailable). It must also accept and pass through the `priority` parameter so skipped-stage records are consistent. Update its signature to `_mark_skipped(version_id, stage, session, priority=0)` and set `priority=priority` on the `IngestionJob` it creates.

- [ ] **Step 6: Update job claiming order in `claim_next_job`**

In `src/harbor_clerk/worker/entry.py`, around line 103-112, update the ORDER BY to include priority:

```python
# Before:
.order_by(stage_priority, IngestionJob.created_at)

# After:
.order_by(IngestionJob.priority, stage_priority, IngestionJob.created_at)
```

- [ ] **Step 7: Add source_path fallback in extract stage**

In `src/harbor_clerk/worker/stages/extract.py`, around line 171, before the existing `storage.get_object` call:

```python
# Before:
storage = get_storage()
response = storage.get_object(version.original_bucket, version.original_object_key)
data = response.read()

# After:
if version.source_path and os.path.exists(version.source_path):
    data = Path(version.source_path).read_bytes()
elif version.original_object_key:
    storage = get_storage()
    response = storage.get_object(version.original_bucket, version.original_object_key)
    data = response.read()
else:
    raise RuntimeError(f"No source for version {version_id}: source_path and original_object_key both empty")
```

Add `import os` and `from pathlib import Path` at top if not already present.

**IMPORTANT:** Also fix the type-dispatch code below the data read (lines 179-231). Multiple lines reference `version.original_object_key` directly for extension checks. These will crash with `AttributeError` when the column is `None`. Add a safe key variable:

```python
# Add after loading data, before type dispatch:
obj_key = (version.original_object_key or version.source_path or "").lower()
```

Then replace all bare `version.original_object_key` references in the dispatch block with `obj_key`:
- Line 179: `version.original_object_key.endswith(".pdf")` → `obj_key.endswith(".pdf")`
- Line 184: `obj_key = version.original_object_key.lower()` → remove (already defined above)
- Line 194: `version.original_object_key.endswith(".rtf")` → `obj_key.endswith(".rtf")`
- Line 201: `version.original_object_key.endswith(".docx")` → `obj_key.endswith(".docx")`
- Line 231: `obj_key.endswith(...)` → already uses the variable, just ensure it's the safe one

- [ ] **Step 8: Add source_path fallback in OCR stage**

In `src/harbor_clerk/worker/stages/ocr.py`, around line 69, same pattern but with guard for None bucket/key:

```python
if version.source_path and os.path.exists(version.source_path):
    data = Path(version.source_path).read_bytes()
elif version.original_object_key:
    storage = get_storage()
    response = storage.get_object(version.original_bucket, version.original_object_key)
    data = response.read()
else:
    raise RuntimeError(f"No source for version {version_id}: source_path and original_object_key both empty")
```

- [ ] **Step 9: Run tests**

```bash
uv run pytest tests/test_watch_pipeline.py -v
```

Expected: all pass.

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/worker/pipeline.py src/harbor_clerk/worker/entry.py src/harbor_clerk/worker/stages/extract.py src/harbor_clerk/worker/stages/ocr.py tests/test_watch_pipeline.py
uv run ruff format src/harbor_clerk/worker/pipeline.py src/harbor_clerk/worker/entry.py src/harbor_clerk/worker/stages/extract.py src/harbor_clerk/worker/stages/ocr.py tests/test_watch_pipeline.py
git add src/harbor_clerk/worker/ tests/test_watch_pipeline.py
git commit -m "feat: priority-aware job claiming and source_path file reading"
```

---

## Task 4: Watch API Endpoints

**Files:**
- Create: `src/harbor_clerk/api/routes/watch.py`
- Modify: `src/harbor_clerk/api/app.py` (register router)
- Modify: `src/harbor_clerk/api/routes/uploads.py` (export ALLOWED_EXTENSIONS)
- Create: `tests/test_watch_api.py`

- [ ] **Step 1: Write failing tests for the watch API**

Create `tests/test_watch_api.py`. These tests use the FastAPI test client pattern established in the codebase. Test the core flows:

```python
"""Tests for watched folder API endpoints."""
import hashlib
import uuid

import pytest
from httpx import AsyncClient


@pytest.fixture
def sample_folder_payload(tmp_path):
    """Use tmp_path so source_path validation passes for test files."""
    return {
        "path": str(tmp_path),
        "bookmark_data": "dGVzdGJvb2ttYXJr",  # base64
        "recursive": True,
    }


@pytest.mark.asyncio
async def test_create_watched_folder(client: AsyncClient, sample_folder_payload):
    resp = await client.post("/api/watch/folders", json=sample_folder_payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["path"] == sample_folder_payload["path"]
    assert data["enabled"] is True
    assert data["folder_id"]


@pytest.mark.asyncio
async def test_list_watched_folders(client: AsyncClient, sample_folder_payload):
    await client.post("/api/watch/folders", json=sample_folder_payload)
    resp = await client.get("/api/watch/folders")
    assert resp.status_code == 200
    folders = resp.json()
    assert len(folders) >= 1


@pytest.mark.asyncio
async def test_delete_watched_folder(client: AsyncClient, sample_folder_payload):
    create_resp = await client.post("/api/watch/folders", json=sample_folder_payload)
    folder_id = create_resp.json()["folder_id"]
    resp = await client.delete(f"/api/watch/folders/{folder_id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_reject_overlapping_folder(client: AsyncClient, tmp_path):
    parent = tmp_path / "docs"
    parent.mkdir()
    child = parent / "sub"
    child.mkdir()
    await client.post("/api/watch/folders", json={
        "path": str(parent),
        "bookmark_data": "dGVzdA==",
    })
    resp = await client.post("/api/watch/folders", json={
        "path": str(child),
        "bookmark_data": "dGVzdDI=",
    })
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_ingest_new_file(client: AsyncClient, tmp_path, sample_folder_payload):
    # Create folder
    create_resp = await client.post("/api/watch/folders", json=sample_folder_payload)
    folder_id = create_resp.json()["folder_id"]

    # Create a temp file to simulate watched file
    test_file = tmp_path / "test.pdf"
    test_file.write_bytes(b"%PDF-1.4 test content")
    sha256 = hashlib.sha256(test_file.read_bytes()).hexdigest()

    resp = await client.post("/api/watch/ingest", json={
        "folder_id": folder_id,
        "relative_path": "test.pdf",
        "sha256": sha256,
        "bookmark_data": "dGVzdGZpbGU=",
        "source_path": str(test_file),
        "mime_type": "application/pdf",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "created"
    assert data["doc_id"]


@pytest.mark.asyncio
async def test_ingest_skip_unchanged(client: AsyncClient, tmp_path, sample_folder_payload):
    create_resp = await client.post("/api/watch/folders", json=sample_folder_payload)
    folder_id = create_resp.json()["folder_id"]

    test_file = tmp_path / "test.pdf"
    test_file.write_bytes(b"%PDF-1.4 test content")
    sha256 = hashlib.sha256(test_file.read_bytes()).hexdigest()

    # First ingest
    await client.post("/api/watch/ingest", json={
        "folder_id": folder_id,
        "relative_path": "test.pdf",
        "sha256": sha256,
        "bookmark_data": "dGVzdGZpbGU=",
        "source_path": str(test_file),
        "mime_type": "application/pdf",
    })

    # Same SHA256 — should skip
    resp = await client.post("/api/watch/ingest", json={
        "folder_id": folder_id,
        "relative_path": "test.pdf",
        "sha256": sha256,
        "bookmark_data": "dGVzdGZpbGU=",
        "source_path": str(test_file),
        "mime_type": "application/pdf",
    })
    assert resp.status_code == 200
    assert resp.json()["action"] == "skipped"


@pytest.mark.asyncio
async def test_remove_watched_file(client: AsyncClient, tmp_path, sample_folder_payload):
    create_resp = await client.post("/api/watch/folders", json=sample_folder_payload)
    folder_id = create_resp.json()["folder_id"]

    test_file = tmp_path / "test.pdf"
    test_file.write_bytes(b"%PDF-1.4 content")
    sha256 = hashlib.sha256(test_file.read_bytes()).hexdigest()

    await client.post("/api/watch/ingest", json={
        "folder_id": folder_id,
        "relative_path": "test.pdf",
        "sha256": sha256,
        "bookmark_data": "dGVzdGZpbGU=",
        "source_path": str(test_file),
        "mime_type": "application/pdf",
    })

    resp = await client.post("/api/watch/remove", json={
        "folder_id": folder_id,
        "relative_path": "test.pdf",
    })
    assert resp.status_code == 200
    assert resp.json()["action"] == "removed"


@pytest.mark.asyncio
async def test_ingest_path_traversal_rejected(client: AsyncClient, sample_folder_payload, tmp_path):
    create_resp = await client.post("/api/watch/folders", json=sample_folder_payload)
    folder_id = create_resp.json()["folder_id"]

    resp = await client.post("/api/watch/ingest", json={
        "folder_id": folder_id,
        "relative_path": "../../etc/passwd",
        "sha256": "a" * 64,
        "bookmark_data": "dGVzdA==",
        "source_path": "/etc/passwd",
        "mime_type": "text/plain",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_allowed_extensions_endpoint(client: AsyncClient):
    resp = await client.get("/api/watch/allowed-extensions")
    assert resp.status_code == 200
    extensions = resp.json()
    assert ".pdf" in extensions
    assert ".docx" in extensions
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_watch_api.py -v
```

Expected: FAIL — routes don't exist yet.

- [ ] **Step 3: Create the watch API routes**

Create `src/harbor_clerk/api/routes/watch.py`. Key implementation details:

**Folder CRUD:**
- `POST /api/watch/folders` — validate no overlapping folders (check if new path is parent/child of existing), create row, return 201
- `GET /api/watch/folders` — list all folders with file counts (subquery on watched_files)
- `PATCH /api/watch/folders/{id}` — update enabled, last_event_id
- `DELETE /api/watch/folders/{id}` — cascade deletes watched_files, soft-delete linked documents
- `POST /api/watch/folders/{id}/rescan` — set last_event_id to NULL, return 200

**Ingest flow** (`POST /api/watch/ingest`):
- Validate source_path is within folder's path: `os.path.realpath(source_path).startswith(os.path.realpath(folder.path))`
- Look up watched_files by (folder_id, relative_path)
- If active + same SHA256 → return `{"action": "skipped"}`
- If active + different SHA256 → create new DocumentVersion (source_path set, original_bucket/key NULL), update latest_version_id, enqueue extract with priority=10, return `{"action": "updated"}`
- If removed row exists → hard-delete it, then create new (fall through to new-file path)
- If new → create Document, DocumentVersion, Upload (source=watch_folder, minio_bucket="", minio_object_key=""), watched_files row, enqueue extract with priority=10, return `{"action": "created"}`

**Remove flow** (`POST /api/watch/remove`):
- Set watched_files.status=removed, removed_at=now()
- Set documents.status=removed
- Return `{"action": "removed"}`

**Rename flow** (`POST /api/watch/rename`):
- Update watched_files.relative_path and bookmark_data
- Update DocumentVersion.source_path
- Return `{"action": "renamed"}`

**Allowed extensions** (`GET /api/watch/allowed-extensions`):
- Import ALLOWED_EXTENSIONS from uploads.py
- Return as JSON array

All endpoints use `Depends(require_user)` for auth.

```python
from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import get_session, require_user
from harbor_clerk.api.routes.uploads import ALLOWED_EXTENSIONS
from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.enums import UploadSource, VersionStatus
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.upload import Upload
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.worker.pipeline import JobStage, enqueue_stage

router = APIRouter(prefix="/api/watch", tags=["watch"])


# --- Schemas ---

class CreateFolderRequest(BaseModel):
    path: str
    bookmark_data: str  # base64-encoded
    recursive: bool = True

class FolderResponse(BaseModel):
    folder_id: str
    path: str
    recursive: bool
    enabled: bool
    last_event_id: int | None
    last_scan_at: str | None
    file_count: int
    created_at: str

class PatchFolderRequest(BaseModel):
    enabled: bool | None = None
    last_event_id: int | None = None

class IngestRequest(BaseModel):
    folder_id: str
    relative_path: str
    sha256: str  # hex-encoded
    bookmark_data: str  # base64
    source_path: str
    mime_type: str

class RemoveRequest(BaseModel):
    folder_id: str
    relative_path: str

class RenameRequest(BaseModel):
    folder_id: str
    old_relative_path: str
    new_relative_path: str
    bookmark_data: str  # base64
    source_path: str

class ActionResponse(BaseModel):
    action: str  # created, updated, skipped, removed, renamed
    doc_id: str | None = None
    version_id: str | None = None


# --- Helpers ---

def _validate_source_path(source_path: str, folder_path: str) -> None:
    real_source = os.path.realpath(source_path)
    real_folder = os.path.realpath(folder_path)
    if not real_source.startswith(real_folder + os.sep) and real_source != real_folder:
        raise HTTPException(400, "source_path is outside the watched folder")


# --- Routes ---

@router.get("/allowed-extensions")
async def get_allowed_extensions(_=Depends(require_user)):
    return sorted(ALLOWED_EXTENSIONS)


@router.get("/folders")
async def list_folders(
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    count_subq = (
        select(func.count(WatchedFile.file_id))
        .where(WatchedFile.folder_id == WatchedFolder.folder_id)
        .where(WatchedFile.status == WatchedFileStatus.active)
        .correlate(WatchedFolder)
        .scalar_subquery()
    )
    result = await session.execute(select(WatchedFolder, count_subq))
    folders = []
    for folder, file_count in result.all():
        folders.append(FolderResponse(
            folder_id=str(folder.folder_id),
            path=folder.path,
            recursive=folder.recursive,
            enabled=folder.enabled,
            last_event_id=folder.last_event_id,
            last_scan_at=folder.last_scan_at.isoformat() if folder.last_scan_at else None,
            file_count=file_count or 0,
            created_at=folder.created_at.isoformat(),
        ))
    return folders


@router.post("/folders", status_code=201)
async def create_folder(
    req: CreateFolderRequest,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    normalized = os.path.realpath(req.path)

    # Check for overlapping folders
    existing = await session.execute(select(WatchedFolder))
    for (folder,) in existing.all():
        existing_real = os.path.realpath(folder.path)
        if normalized.startswith(existing_real + os.sep) or existing_real.startswith(normalized + os.sep) or normalized == existing_real:
            raise HTTPException(409, f"Overlaps with existing watched folder: {folder.path}")

    folder = WatchedFolder(
        folder_id=uuid.uuid4(),
        path=normalized,
        bookmark_data=base64.b64decode(req.bookmark_data),
        recursive=req.recursive,
    )
    session.add(folder)
    await session.commit()
    return FolderResponse(
        folder_id=str(folder.folder_id),
        path=folder.path,
        recursive=folder.recursive,
        enabled=folder.enabled,
        last_event_id=folder.last_event_id,
        last_scan_at=None,
        file_count=0,
        created_at=folder.created_at.isoformat(),
    )


@router.patch("/folders/{folder_id}")
async def patch_folder(
    folder_id: uuid.UUID,
    req: PatchFolderRequest,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    folder = await session.get(WatchedFolder, folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    if req.enabled is not None:
        folder.enabled = req.enabled
    if req.last_event_id is not None:
        folder.last_event_id = req.last_event_id
    await session.commit()
    return {"status": "updated"}


@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    folder = await session.get(WatchedFolder, folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")

    # Soft-delete all linked documents
    files_result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )
    for (wf,) in files_result.all():
        if wf.doc_id:
            await session.execute(
                update(Document).where(Document.doc_id == wf.doc_id).values(status="removed")
            )

    await session.delete(folder)  # cascades to watched_files
    await session.commit()


@router.post("/folders/{folder_id}/rescan")
async def rescan_folder(
    folder_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    folder = await session.get(WatchedFolder, folder_id)
    if not folder:
        raise HTTPException(404, "Folder not found")
    folder.last_event_id = None
    await session.commit()
    return {"status": "rescan_requested"}


@router.post("/ingest")
async def ingest_file(
    req: IngestRequest,
    session: AsyncSession = Depends(get_session),
    principal=Depends(require_user),
):
    folder_id = uuid.UUID(req.folder_id)
    folder = await session.get(WatchedFolder, folder_id)
    if not folder:
        raise HTTPException(404, "Watched folder not found")

    _validate_source_path(req.source_path, folder.path)

    sha256_bytes = bytes.fromhex(req.sha256)

    # Look up existing watched file
    existing = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.relative_path == req.relative_path,
        )
    )
    wf = existing.scalar_one_or_none()

    if wf and wf.status == WatchedFileStatus.active and wf.sha256 == sha256_bytes:
        return ActionResponse(action="skipped")

    if wf and wf.status == WatchedFileStatus.removed:
        # Resurrection: check if same bookmark (file restored from Trash)
        incoming_bookmark = base64.b64decode(req.bookmark_data)
        if wf.bookmark_data == incoming_bookmark and wf.doc_id:
            # Same physical file — reactivate instead of creating new
            wf.status = WatchedFileStatus.active
            wf.removed_at = None
            wf.bookmark_data = incoming_bookmark
            wf.updated_at = datetime.now(timezone.utc)
            # Re-activate the document
            await session.execute(
                update(Document).where(Document.doc_id == wf.doc_id).values(status="active")
            )
            # Update source_path (may differ after Trash restore)
            if wf.version_id:
                version = await session.get(DocumentVersion, wf.version_id)
                if version:
                    version.source_path = req.source_path
            if wf.sha256 != sha256_bytes:
                # Content changed during removal period — re-ingest
                wf.sha256 = sha256_bytes
                await session.commit()
                enqueue_stage(wf.version_id, JobStage.extract, priority=10)
                return ActionResponse(action="updated", doc_id=str(wf.doc_id), version_id=str(wf.version_id))
            await session.commit()
            return ActionResponse(action="resurrected", doc_id=str(wf.doc_id), version_id=str(wf.version_id))
        # Different bookmark — hard-delete stale row, treat as new file
        await session.delete(wf)
        await session.flush()
        wf = None

    if wf and wf.status == WatchedFileStatus.active:
        # Content changed — create new version, keep old until finalize
        new_version = DocumentVersion(
            version_id=uuid.uuid4(),
            doc_id=wf.doc_id,
            original_sha256=sha256_bytes,
            original_bucket=None,
            original_object_key=None,
            mime_type=req.mime_type,
            size_bytes=os.path.getsize(req.source_path) if os.path.exists(req.source_path) else None,
            status=VersionStatus.queued,
            source_path=req.source_path,
        )
        session.add(new_version)
        await session.flush()

        # Update document to point to new version
        await session.execute(
            update(Document)
            .where(Document.doc_id == wf.doc_id)
            .values(latest_version_id=new_version.version_id)
        )

        wf.sha256 = sha256_bytes
        wf.version_id = new_version.version_id
        wf.bookmark_data = base64.b64decode(req.bookmark_data)
        wf.updated_at = datetime.now(timezone.utc)
        await session.commit()

        enqueue_stage(new_version.version_id, JobStage.extract, priority=10)

        return ActionResponse(
            action="updated",
            doc_id=str(wf.doc_id),
            version_id=str(new_version.version_id),
        )

    # New file
    title = req.relative_path
    # Use just the filename as title, but prefix with dir if nested
    parts = req.relative_path.rsplit("/", 1)
    if len(parts) == 1:
        title = parts[0]
    else:
        title = parts[-1]  # filename only; relative_path preserved for context

    doc = Document(
        doc_id=uuid.uuid4(),
        title=os.path.splitext(title)[0],
        canonical_filename=title,
    )
    session.add(doc)
    await session.flush()

    version = DocumentVersion(
        version_id=uuid.uuid4(),
        doc_id=doc.doc_id,
        original_sha256=sha256_bytes,
        original_bucket=None,
        original_object_key=None,
        mime_type=req.mime_type,
        size_bytes=os.path.getsize(req.source_path) if os.path.exists(req.source_path) else None,
        status=VersionStatus.queued,
        source_path=req.source_path,
    )
    session.add(version)
    await session.flush()

    doc.latest_version_id = version.version_id

    upload = Upload(
        upload_id=uuid.uuid4(),
        user_id=principal.id,
        source=UploadSource.watch_folder,
        original_filename=title,
        mime_type=req.mime_type,
        size_bytes=version.size_bytes,
        sha256=sha256_bytes,
        minio_bucket="",
        minio_object_key="",
        doc_id=doc.doc_id,
        version_id=version.version_id,
        source_path=req.source_path,
        status="processing",
    )
    session.add(upload)

    new_wf = WatchedFile(
        file_id=uuid.uuid4(),
        folder_id=folder_id,
        relative_path=req.relative_path,
        bookmark_data=base64.b64decode(req.bookmark_data),
        sha256=sha256_bytes,
        doc_id=doc.doc_id,
        version_id=version.version_id,
    )
    session.add(new_wf)
    await session.commit()

    enqueue_stage(version.version_id, JobStage.extract, priority=10)

    return ActionResponse(
        action="created",
        doc_id=str(doc.doc_id),
        version_id=str(version.version_id),
    )


@router.post("/remove")
async def remove_file(
    req: RemoveRequest,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    folder_id = uuid.UUID(req.folder_id)
    result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.relative_path == req.relative_path,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(404, "Watched file not found")

    wf.status = WatchedFileStatus.removed
    wf.removed_at = datetime.now(timezone.utc)

    if wf.doc_id:
        await session.execute(
            update(Document).where(Document.doc_id == wf.doc_id).values(status="removed")
        )
    await session.commit()
    return ActionResponse(action="removed")


@router.post("/rename")
async def rename_file(
    req: RenameRequest,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_user),
):
    folder_id = uuid.UUID(req.folder_id)
    folder = await session.get(WatchedFolder, folder_id)
    if not folder:
        raise HTTPException(404, "Watched folder not found")

    _validate_source_path(req.source_path, folder.path)

    result = await session.execute(
        select(WatchedFile).where(
            WatchedFile.folder_id == folder_id,
            WatchedFile.relative_path == req.old_relative_path,
            WatchedFile.status == WatchedFileStatus.active,
        )
    )
    wf = result.scalar_one_or_none()
    if not wf:
        raise HTTPException(404, "Watched file not found")

    wf.relative_path = req.new_relative_path
    wf.bookmark_data = base64.b64decode(req.bookmark_data)

    # Update source_path on the version
    if wf.version_id:
        version = await session.get(DocumentVersion, wf.version_id)
        if version:
            version.source_path = req.source_path

    await session.commit()
    return ActionResponse(action="renamed")
```

- [ ] **Step 4: Register the router**

In `src/harbor_clerk/api/app.py`, import and include the watch router alongside the existing router includes:

```python
from harbor_clerk.api.routes.watch import router as watch_router
app.include_router(watch_router)
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_watch_api.py -v
```

Expected: all pass. (Note: tests may need adjustment based on the existing test fixtures — match the existing conftest.py patterns for `client` fixture and auth setup.)

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/api/routes/watch.py tests/test_watch_api.py
uv run ruff format src/harbor_clerk/api/routes/watch.py tests/test_watch_api.py
git add src/harbor_clerk/api/routes/watch.py src/harbor_clerk/api/app.py tests/test_watch_api.py
git commit -m "feat: watch API endpoints for folder CRUD, ingest, remove, rename"
```

---

## Task 5: Reaper and Download Integration

**Files:**
- Modify: `src/harbor_clerk/api/app.py:37-124` (reaper loop)
- Modify: `src/harbor_clerk/api/routes/documents.py:746-787` (download endpoint)
- Modify: `src/harbor_clerk/api/routes/documents.py:48-171` (list endpoint — add watch fields)

- [ ] **Step 1: Add 30-day reaper for removed watched files**

In `src/harbor_clerk/api/app.py`, inside the `_session_reaper_loop` function, after the existing cleanup blocks (around line 107), add:

```python
# --- Watched files: hard-delete after 30-day grace period ---
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus
cutoff = datetime.now(timezone.utc) - timedelta(days=30)
expired_result = await reaper_session.execute(
    select(WatchedFile).where(
        WatchedFile.status == WatchedFileStatus.removed,
        WatchedFile.removed_at < cutoff,
    )
)
expired = expired_result.scalars().all()
for wf in expired:
    if wf.doc_id:
        doc = await reaper_session.get(Document, wf.doc_id)
        if doc:
            await reaper_session.delete(doc)  # cascades to versions, chunks, embeddings
    await reaper_session.delete(wf)
if expired:
    logger.info("Reaped %d expired watched files", len(expired))
    await reaper_session.commit()
```

Note: adapt this to match the existing reaper's session management pattern (it may use a different variable name for the session).

- [ ] **Step 2: Update download endpoint for source_path**

In `src/harbor_clerk/api/routes/documents.py`, around line 772 where it calls `storage.get_object()`, add the source_path check:

```python
# Before:
storage = get_storage()
response = storage.get_object(version.original_bucket, version.original_object_key)
# ...return StreamingResponse

# After:
if version.source_path and os.path.exists(version.source_path):
    from pathlib import Path
    file_bytes = Path(version.source_path).read_bytes()
    # Return with same headers as storage path
else:
    storage = get_storage()
    response = storage.get_object(version.original_bucket, version.original_object_key)
    file_bytes = response.read()
# ...return Response(content=file_bytes, ...)
```

Adapt to match the existing response pattern (StreamingResponse vs Response).

- [ ] **Step 3: Add watch fields to document list response**

In `src/harbor_clerk/api/routes/documents.py`, in the documents list endpoint response building:

- Add a left join on `watched_files` by `doc_id`
- Include `watch_source_path` (from watched_files.relative_path + folder.path) and `watch_status` (from watched_files.status) in the DocumentSummary response
- These are nullable — only set for documents linked to watched files

- [ ] **Step 4: Lint and commit**

```bash
uv run ruff check src/harbor_clerk/api/app.py src/harbor_clerk/api/routes/documents.py
uv run ruff format src/harbor_clerk/api/app.py src/harbor_clerk/api/routes/documents.py
git add src/harbor_clerk/api/app.py src/harbor_clerk/api/routes/documents.py
git commit -m "feat: reaper for expired watched files, download/list integration"
```

---

## Task 6: Frontend Changes

**Files:**
- Modify: `frontend/src/pages/UploadPage.tsx` (macOS hint)
- Modify: `frontend/src/pages/DocumentsPage.tsx` (watch indicator, removed status)

- [ ] **Step 1: Add macOS hint to upload page**

In `frontend/src/pages/UploadPage.tsx`, add a helper and a banner below the upload dropzone:

```typescript
const isNativeApp = !!window.webkit?.messageHandlers;
```

Render conditionally when `isNativeApp` is true:

```tsx
{isNativeApp && (
  <div className="mt-4 rounded-lg border border-blue-500/20 bg-blue-500/5 px-4 py-3 text-sm text-gray-400">
    <span className="font-medium text-blue-400">Tip:</span> Set up watched folders in
    Harbor Clerk Server preferences to automatically ingest files from your Mac.
  </div>
)}
```

Add TypeScript declaration if needed for `window.webkit`:

```typescript
declare global {
  interface Window {
    webkit?: { messageHandlers?: unknown };
  }
}
```

- [ ] **Step 2: Add watch indicators to documents page**

In `frontend/src/pages/DocumentsPage.tsx`, in the document row rendering:

- Check for `watch_source_path` on the document summary
- If present, render a small link icon (use an SVG or existing icon) with tooltip showing the path
- If `watch_status === 'removed'`, render row with muted opacity and "Source file removed" text

```tsx
{doc.watch_source_path && (
  <span className="ml-2 text-gray-500" title={`Watched: ${doc.watch_source_path}`}>
    <LinkIcon className="inline h-3.5 w-3.5" />
  </span>
)}
{doc.watch_status === 'removed' && (
  <span className="ml-2 text-xs text-red-400/60">Source file removed</span>
)}
```

- [ ] **Step 3: Update TypeScript types**

Add `watch_source_path` and `watch_status` to the document summary type:

```typescript
watch_source_path?: string | null;
watch_status?: 'active' | 'removed' | null;
```

- [ ] **Step 4: Lint and commit**

```bash
cd /Users/alex/mcp-gateway/frontend
npm run lint && npm run type-check
cd /Users/alex/mcp-gateway
git add frontend/src/pages/UploadPage.tsx frontend/src/pages/DocumentsPage.tsx
git commit -m "feat: macOS watched folder hint and document watch indicators"
```

---

## Task 7: Swift — WatchedFolderManager

**Files:**
- Create: `macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift`
- Modify: `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift:198-259` (start watcher after API ready)

This is the largest task. The Swift implementation covers:

1. Fetching watched folder config from the API
2. Starting FSEventStreams per folder
3. UTType-based file filtering
4. SHA256 hashing
5. Bookmark creation/resolution
6. Calling the Python API for ingest/remove/rename
7. Event ID persistence

- [ ] **Step 1: Create WatchedFolderManager.swift**

```swift
import Foundation
import UniformTypeIdentifiers
import CryptoKit

/// Monitors filesystem directories and syncs changes to the Harbor Clerk API.
@MainActor
final class WatchedFolderManager {
    static let shared = WatchedFolderManager()

    private var watchers: [UUID: FolderWatcher] = [:]
    private var allowedExtensions: Set<String> = []
    private var apiBaseURL: String = ""
    private var authToken: String = ""

    private init() {}

    // MARK: - Lifecycle

    func start(apiBaseURL: String, authToken: String) {
        self.apiBaseURL = apiBaseURL
        self.authToken = authToken

        Task {
            await fetchAllowedExtensions()
            await fetchAndStartWatchers()
        }
    }

    func stop() {
        for watcher in watchers.values {
            watcher.stop()
        }
        watchers.removeAll()
    }

    // MARK: - API Communication

    private func fetchAllowedExtensions() async {
        // GET /api/watch/allowed-extensions
        // Parse JSON array into allowedExtensions set
        // ... (standard URLSession call with authToken header)
    }

    private func fetchAndStartWatchers() async {
        // GET /api/watch/folders
        // For each enabled folder: resolve bookmark, start FolderWatcher
        // For folders with no last_event_id: trigger full scan first
    }

    func addFolder(url: URL) async throws {
        // Create bookmark data from URL
        let bookmarkData = try url.bookmarkData(
            options: [],
            includingResourceValuesForKeys: [.contentTypeKey],
            relativeTo: nil
        )

        // POST /api/watch/folders with path + base64(bookmarkData)
        // Start watcher for the new folder
    }

    func removeFolder(folderId: UUID) async throws {
        watchers[folderId]?.stop()
        watchers.removeValue(forKey: folderId)
        // DELETE /api/watch/folders/{folderId}
    }

    func rescanFolder(folderId: UUID) async throws {
        // POST /api/watch/folders/{folderId}/rescan
        // Stop current watcher, do full walk, restart with streaming
    }

    // MARK: - File Type Checking

    func isFileAllowed(url: URL) -> Bool {
        // 1. Try UTType from resource values
        if let resourceValues = try? url.resourceValues(forKeys: [.contentTypeKey]),
           let uttype = resourceValues.contentType {
            // Check against allowed supertypes
            let allowedTypes: [UTType] = [
                .pdf, .image, .plainText, .rtf, .html, .emailMessage, .epub,
                .spreadsheet, .presentation, .commaSeparatedText,
            ]
            for allowed in allowedTypes {
                if uttype.conforms(to: allowed) { return true }
            }
            // Check specific Office types
            let officeIdentifiers = [
                "org.openxmlformats.wordprocessingml.document",     // .docx
                "com.microsoft.word.doc",                           // .doc
                "org.oasis-open.opendocument.text",                 // .odt
                "org.openxmlformats.spreadsheetml.sheet",           // .xlsx
                "org.openxmlformats.presentationml.presentation",   // .pptx
                "com.apple.keynote.key",                            // .key
                "com.apple.iwork.pages.sffpages",                   // .pages
                "com.apple.iwork.numbers.sffnumbers",               // .numbers
            ]
            for id in officeIdentifiers {
                if let known = UTType(id), uttype.conforms(to: known) { return true }
            }
        }

        // 2. Fallback to extension
        let ext = "." + url.pathExtension.lowercased()
        return allowedExtensions.contains(ext)
    }

    // MARK: - File Hashing

    func sha256(of url: URL) -> String? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - Bookmark Helpers

    func createBookmark(for url: URL) -> Data? {
        try? url.bookmarkData(options: [], includingResourceValuesForKeys: nil, relativeTo: nil)
    }

    func resolveBookmark(_ data: Data) -> URL? {
        var isStale = false
        return try? URL(resolvingBookmarkData: data, options: [], relativeTo: nil, bookmarkDataIsStale: &isStale)
    }
}


// MARK: - FolderWatcher (FSEventStream wrapper)

final class FolderWatcher {
    let folderId: UUID
    let path: String
    private var stream: FSEventStreamRef?
    private let callback: (UUID, String, FSEventStreamEventFlags) -> Void

    init(folderId: UUID, path: String, sinceEventId: FSEventStreamEventId,
         callback: @escaping (UUID, String, FSEventStreamEventFlags) -> Void) {
        self.folderId = folderId
        self.path = path
        self.callback = callback

        let context = Unmanaged.passUnretained(self).toOpaque()
        var ctx = FSEventStreamContext(
            version: 0, info: context, retain: nil, release: nil, copyDescription: nil
        )

        let paths = [path] as CFArray
        stream = FSEventStreamCreate(
            nil,
            { _, info, numEvents, eventPaths, eventFlags, eventIds in
                guard let info = info else { return }
                let watcher = Unmanaged<FolderWatcher>.fromOpaque(info).takeUnretainedValue()
                let paths = Unmanaged<CFArray>.fromOpaque(eventPaths).takeUnretainedValue() as! [String]
                for i in 0..<numEvents {
                    watcher.callback(watcher.folderId, paths[i], eventFlags[i])
                }
            },
            &ctx,
            paths,
            sinceEventId,
            1.0,  // latency: coalesce events over 1 second
            FSEventStreamCreateFlags(
                kFSEventStreamCreateFlagFileEvents |
                kFSEventStreamCreateFlagUseCachedEvents
            )
        )

        if let stream = stream {
            FSEventStreamScheduleWithRunLoop(stream, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
            FSEventStreamStart(stream)
        }
    }

    /// Must be called on the main thread (same run loop the stream was scheduled on).
    /// Always call stop() explicitly before releasing — do NOT rely on deinit.
    func stop() {
        dispatchPrecondition(condition: .onQueue(.main))
        if let stream = stream {
            FSEventStreamStop(stream)
            FSEventStreamInvalidate(stream)
            FSEventStreamRelease(stream)
        }
        stream = nil
    }
}
```

This is a skeleton — the full implementation will fill in the API communication methods (URLSession calls with JSON encoding/decoding), the event handling callback that distinguishes create/modify/rename/delete, and the full-scan walk for new folders.

- [ ] **Step 2: Wire into ServiceManager**

In `ServiceManager.swift`, after the API service is confirmed healthy (around line 245-253), add:

```swift
// Start watched folder monitoring
WatchedFolderManager.shared.start(
    apiBaseURL: "http://127.0.0.1:\(settings.apiPort)",
    authToken: authToken  // from keychain or local admin credentials
)
```

In the `stopAll()` method, add:

```swift
WatchedFolderManager.shared.stop()
```

- [ ] **Step 3: Build and verify**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -configuration Debug build 2>&1 | tail -5
```

Expected: BUILD SUCCEEDED

- [ ] **Step 4: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift
git add macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift
git commit -m "feat: WatchedFolderManager with FSEvents and UTType filtering"
```

---

## Task 8: Swift — Preferences UI

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift`

- [ ] **Step 1: Add Watched Folders section**

In `PreferencesWindow.swift`, after the existing settings sections (around line 141), add a new section:

```swift
// --- Watched Folders ---
Section("Watched Folders") {
    ForEach(watchedFolders) { folder in
        HStack {
            Image(systemName: "folder")
                .foregroundColor(.blue)
            VStack(alignment: .leading, spacing: 2) {
                Text(folder.path)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text("\(folder.fileCount) files \u{2022} Last scan: \(folder.lastScanFormatted)")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Spacer()
            Toggle("", isOn: Binding(
                get: { folder.enabled },
                set: { newValue in Task { await toggleFolder(folder.id, enabled: newValue) } }
            ))
            .labelsHidden()
            Button("Rescan") {
                Task { await rescanFolder(folder.id) }
            }
            .buttonStyle(.borderless)
            Button(role: .destructive) {
                Task { await removeFolder(folder.id) }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
        }
    }
    Button("Add Folder...") {
        showFolderPicker()
    }
}
```

State variables:
```swift
@State private var watchedFolders: [WatchedFolderInfo] = []
```

Helper struct:
```swift
struct WatchedFolderInfo: Identifiable {
    let id: UUID
    let path: String
    var enabled: Bool
    let fileCount: Int
    let lastScanAt: Date?
    var lastScanFormatted: String {
        guard let date = lastScanAt else { return "Never" }
        return date.formatted(.relative(presentation: .named))
    }
}
```

Methods:
- `showFolderPicker()` — opens `NSOpenPanel` (directory mode), calls `WatchedFolderManager.shared.addFolder(url:)`
- `toggleFolder(id:enabled:)` — calls API PATCH
- `rescanFolder(id:)` — calls `WatchedFolderManager.shared.rescanFolder(folderId:)`
- `removeFolder(id:)` — calls `WatchedFolderManager.shared.removeFolder(folderId:)`
- `loadFolders()` — called on appear, fetches from API

- [ ] **Step 2: Build and verify**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -configuration Debug build 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift
git commit -m "feat: watched folders section in preferences UI"
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update README.md**

In the **Architecture** section (around line 168), add a subsection after "Document Intelligence":

```markdown
### Watched Folders (macOS)

The native macOS app can monitor filesystem directories for changes. Files added to watched folders
are automatically ingested without copying — Harbor Clerk references them in place. Renames, modifications,
and deletions are tracked via macOS bookmark data and FSEvents. Configure in Harbor Clerk Server preferences.
```

In the **Deployment Options** section, add to the macOS column:

```markdown
- Watched folders (auto-ingest from local directories)
```

- [ ] **Step 2: Update CLAUDE.md**

Add to the Architecture section, under "macOS Native Apps":

```markdown
- **Watched Folders** (macOS only): FSEvents-based directory monitoring with macOS bookmark data for file identity tracking. Files referenced in place (not copied). UTType-based file detection. 30-day soft-delete reaper. Priority-aware job queue (GUI uploads preempt watched folder ingestion). Config via native preferences, state in PostgreSQL.
```

Add to "Key API Surface":

```markdown
- Watch: `/api/watch/folders` (CRUD), `/api/watch/ingest`, `/api/watch/remove`, `/api/watch/rename`
```

Add to "Database" section:

```markdown
Additional tables: `watched_folders`, `watched_files` (macOS watched directory tracking).
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document watched folders feature in README and CLAUDE.md"
```

---

## Task 10: Integration Testing and Final Verification

- [ ] **Step 1: Run all Python tests**

```bash
cd /Users/alex/mcp-gateway
uv run pytest tests/ -v --tb=short
```

Expected: all pass, including new test_watch_api.py and test_watch_pipeline.py.

- [ ] **Step 2: Run Python linting**

```bash
uv run ruff check .
uv run ruff format --check .
```

- [ ] **Step 3: Run frontend checks**

```bash
cd /Users/alex/mcp-gateway/frontend
npm run lint && npm run type-check && npm run format:check
```

- [ ] **Step 4: Build macOS apps**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -configuration Debug build 2>&1 | tail -5
```

- [ ] **Step 5: Manual smoke test**

1. Launch the macOS app
2. Open preferences → add a watched folder
3. Drop a PDF into the folder → verify it appears in Documents
4. Rename the file in Finder → verify document tracks it
5. Delete the file → verify document shows as removed
6. Upload a file via GUI while watched folder is scanning → verify GUI upload processes first

- [ ] **Step 6: Final commit and PR**

```bash
git add -A
git status
# If any uncommitted changes remain, commit them
# Then create feature branch and PR
```
