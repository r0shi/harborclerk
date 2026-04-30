# Watched-Folder-First Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the macOS-only Swift FSEvents watcher with a cross-platform Python `watchdog`-based daemon, bring watched folders to Docker, and graduate folder management UI from the macOS menubar into the main web app at a new `/folders` route.

**Architecture:** New `harbor-clerk-watcher` entry point launches a `watchdog.observers.Observer` per watched folder (with `PollingObserver` fallback for filesystems where native observers fail). On macOS the watcher is a managed subprocess of the Server menubar app. On Docker it's a new compose service. Folder management API surface stays on `/api/watch/folders/*` with new endpoints for system shape, per-folder progress, and an SSE stream. Web UI replaces the existing "Upload" tab with "Folders".

**Tech Stack:** Python 3.12, `watchdog` library, FastAPI, SQLAlchemy 2.0 async, Alembic, PostgreSQL `LISTEN/NOTIFY`, React 19, Swift (WKWebView JS bridge).

**Spec:** [`docs/superpowers/specs/2026-04-30-watched-folder-first-stage-1-design.md`](../specs/2026-04-30-watched-folder-first-stage-1-design.md)

---

## File Structure

**New files:**
- `alembic/versions/0016_watcher_unification.py` — schema migration
- `src/harbor_clerk/watcher/__init__.py`
- `src/harbor_clerk/watcher/main.py` — entry point + signal handling + observer lifecycle
- `src/harbor_clerk/watcher/observer.py` — `watchdog.Observer` wrapper with per-folder polling fallback
- `src/harbor_clerk/watcher/events.py` — translates filesystem events → DB writes + ingestion-job enqueues
- `src/harbor_clerk/watcher/discovery.py` — Docker auto-discovery loop (60s rescan of `WATCH_ROOT`)
- `src/harbor_clerk/watcher/db_listener.py` — `LISTEN watched_folders_changed` to react to folder add/remove
- `src/harbor_clerk/watcher/notify.py` — helper to fire `NOTIFY watched_folders_changed` from API routes
- `tests/watcher/__init__.py`
- `tests/watcher/test_events.py`
- `tests/watcher/test_observer.py`
- `tests/watcher/test_discovery.py`
- `tests/watcher/test_main.py`
- `frontend/src/pages/FoldersPage.tsx`
- `frontend/src/hooks/useFolderProgress.ts`
- `docs/watched-folders-docker.md` — operator docs for `volumes:` model

**Modified files:**
- `pyproject.toml` — new entry point + `watchdog` dep
- `docker-compose.yml` — new `watcher` service
- `src/harbor_clerk/models/watched.py` — new columns on `WatchedFolder`
- `src/harbor_clerk/api/routes/watch.py` — new endpoints (`/system`, `/folders/{id}/progress`, `/folders/stream`), tightened POST validation, DELETE semantics
- `src/harbor_clerk/api/routes/__init__.py` — no change expected, but verify route module wiring
- `src/harbor_clerk/config.py` — add `watch_root` setting (defaults to `""` on macOS, `/data/watch` on Docker)
- `frontend/src/App.tsx` — replace Upload tab with Folders, route `/folders` to FoldersPage
- `frontend/src/pages/DocumentsPage.tsx` — add Folder column + filter chip
- `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — add `WatcherService`, remove `WatchedFolderManager` plumbing
- `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift` — remove "Watched Folders" section
- `macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift` — **DELETE**
- `macos/HarborClerk/HarborClerk/HarborClerkApp.swift` (or wherever the WKWebView lives) — register `pickFolder` `WKScriptMessageHandler`
- `docker/app.Dockerfile` — same image used by `watcher` service; verify `watchdog` is installed via `uv sync`

---

## Task 1: Schema migration + model update

**Files:**
- Create: `alembic/versions/0016_watcher_unification.py`
- Modify: `src/harbor_clerk/models/watched.py:17-29`
- Test: `tests/watcher/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/watcher/test_schema.py
import pytest
from sqlalchemy import inspect
from harbor_clerk.db_sync import get_sync_session


@pytest.mark.usefixtures("alembic_upgraded")
def test_watched_folders_has_new_columns():
    session = get_sync_session()
    try:
        insp = inspect(session.bind)
        cols = {c["name"]: c for c in insp.get_columns("watched_folders")}
        assert "unavailable_reason" in cols
        assert "display_name" in cols
        assert "auto_discovered" in cols
        assert cols["bookmark_data"]["nullable"] is True
        assert cols["auto_discovered"]["nullable"] is False
    finally:
        session.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/watcher/test_schema.py -v`
Expected: FAIL — columns missing or nullability wrong.

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0016_watcher_unification.py
"""Watcher unification: nullable bookmark_data + display_name + unavailable_reason + auto_discovered.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("watched_folders", "bookmark_data", nullable=True)
    op.add_column("watched_folders", sa.Column("unavailable_reason", sa.Text(), nullable=True))
    op.add_column("watched_folders", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column(
        "watched_folders",
        sa.Column("auto_discovered", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("watched_folders", "auto_discovered")
    op.drop_column("watched_folders", "display_name")
    op.drop_column("watched_folders", "unavailable_reason")
    op.alter_column("watched_folders", "bookmark_data", nullable=False)
```

- [ ] **Step 4: Update the SQLAlchemy model**

```python
# src/harbor_clerk/models/watched.py — replace the WatchedFolder class
class WatchedFolder(Base):
    __tablename__ = "watched_folders"

    folder_id: Mapped[uuid_pk]
    path: Mapped[str] = mapped_column(Text, nullable=False)
    bookmark_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    recursive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    last_event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unavailable_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_discovered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[created_at]

    files: Mapped[list["WatchedFile"]] = relationship(back_populates="folder", cascade="all, delete-orphan")
```

- [ ] **Step 5: Run migration and test**

Run: `uv run alembic upgrade head && uv run pytest tests/watcher/test_schema.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/0016_watcher_unification.py src/harbor_clerk/models/watched.py tests/watcher/test_schema.py tests/watcher/__init__.py
git commit -m "feat(watcher): migration 0016 — display_name, auto_discovered, unavailable_reason"
```

---

## Task 2: Config — `watch_root` setting

**Files:**
- Modify: `src/harbor_clerk/config.py`
- Test: `tests/test_config.py` (add to existing)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py — add at bottom
def test_watch_root_default_empty():
    from harbor_clerk.config import Settings
    s = Settings()
    assert s.watch_root == ""


def test_watch_root_from_env(monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "/data/watch")
    from harbor_clerk.config import Settings
    s = Settings()
    assert s.watch_root == "/data/watch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v -k watch_root`
Expected: FAIL — `watch_root` attribute does not exist.

- [ ] **Step 3: Add `watch_root` to Settings**

In `src/harbor_clerk/config.py`, in the `Settings` class:

```python
    watch_root: str = Field(default="")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -v -k watch_root`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/config.py tests/test_config.py
git commit -m "feat(config): add WATCH_ROOT setting"
```

---

## Task 3: Watcher events module — DB-write translation

**Files:**
- Create: `src/harbor_clerk/watcher/__init__.py` (empty)
- Create: `src/harbor_clerk/watcher/events.py`
- Test: `tests/watcher/test_events.py`

This module is pure: takes a synthetic event, performs the right `watched_files` upsert + ingestion-job enqueue. No `watchdog` dependency leaks here — all events are passed in as a small dataclass.

- [ ] **Step 1: Write the failing test**

```python
# tests/watcher/test_events.py
import hashlib
import uuid
from pathlib import Path

import pytest

from harbor_clerk.watcher.events import FileEvent, EventKind, handle_event
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.ingestion_job import IngestionJob


@pytest.fixture
def folder(sync_session):
    f = WatchedFolder(path="/tmp/test", display_name="test")
    sync_session.add(f)
    sync_session.commit()
    return f


def test_created_file_creates_watched_file_and_extract_job(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"hello world")
    event = FileEvent(
        kind=EventKind.created,
        folder_id=folder.folder_id,
        relative_path="doc.pdf",
        absolute_path=str(f),
    )
    handle_event(sync_session, event)
    sync_session.commit()

    wf = sync_session.query(WatchedFile).filter_by(folder_id=folder.folder_id).one()
    assert wf.relative_path == "doc.pdf"
    assert wf.sha256 == hashlib.sha256(b"hello world").digest()
    assert wf.status == WatchedFileStatus.active

    job = sync_session.query(IngestionJob).filter_by(version_id=wf.version_id, stage="extract").one()
    assert job.status == "queued"


def test_modified_file_with_new_sha_creates_new_version(sync_session, folder, tmp_path):
    # First create
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"v1")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    v1 = sync_session.query(WatchedFile).one().version_id

    # Modify
    f.write_bytes(b"v2")
    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.version_id != v1
    assert wf.sha256 == hashlib.sha256(b"v2").digest()


def test_deleted_file_marks_watched_file_removed(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    f.unlink()
    handle_event(sync_session, FileEvent(EventKind.deleted, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.status == WatchedFileStatus.removed
    assert wf.removed_at is not None


def test_modified_with_same_sha_is_noop(sync_session, folder, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"unchanged")
    handle_event(sync_session, FileEvent(EventKind.created, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()
    v1 = sync_session.query(WatchedFile).one().version_id

    handle_event(sync_session, FileEvent(EventKind.modified, folder.folder_id, "doc.pdf", str(f)))
    sync_session.commit()

    wf = sync_session.query(WatchedFile).one()
    assert wf.version_id == v1  # no new version
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/watcher/test_events.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write `events.py`**

```python
# src/harbor_clerk/watcher/events.py
"""Filesystem-event → database-write translation.

Pure module: no watchdog imports, no I/O scheduling. Caller passes in
synthetic FileEvent records; this module does the database work.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.models.document import Document
from harbor_clerk.models.document_version import DocumentVersion
from harbor_clerk.models.ingestion_job import IngestionJob
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus


class EventKind(str, Enum):
    created = "created"
    modified = "modified"
    deleted = "deleted"


@dataclass
class FileEvent:
    kind: EventKind
    folder_id: uuid.UUID
    relative_path: str
    absolute_path: str


def _sha256_of(path: str) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def handle_event(session: Session, event: FileEvent) -> None:
    """Apply a FileEvent to the database. Caller is responsible for commit."""
    existing = (
        session.query(WatchedFile)
        .filter_by(folder_id=event.folder_id, relative_path=event.relative_path)
        .one_or_none()
    )

    if event.kind == EventKind.deleted:
        if existing and existing.status == WatchedFileStatus.active:
            existing.status = WatchedFileStatus.removed
            existing.removed_at = datetime.now(timezone.utc)
        return

    sha = _sha256_of(event.absolute_path)

    if existing and existing.sha256 == sha and existing.status == WatchedFileStatus.active:
        return  # no-op

    # New file or content changed → create a new doc + version + extract job
    doc = Document()
    session.add(doc)
    session.flush()
    version = DocumentVersion(
        doc_id=doc.doc_id,
        source_path=event.absolute_path,
    )
    session.add(version)
    session.flush()
    doc.latest_version_id = version.version_id

    if existing is None:
        wf = WatchedFile(
            folder_id=event.folder_id,
            relative_path=event.relative_path,
            sha256=sha,
            doc_id=doc.doc_id,
            version_id=version.version_id,
            status=WatchedFileStatus.active,
            bookmark_data=b"",
        )
        session.add(wf)
    else:
        existing.sha256 = sha
        existing.doc_id = doc.doc_id
        existing.version_id = version.version_id
        existing.status = WatchedFileStatus.active
        existing.removed_at = None

    session.add(IngestionJob(version_id=version.version_id, stage="extract", status="queued"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/watcher/test_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/__init__.py src/harbor_clerk/watcher/events.py tests/watcher/test_events.py
git commit -m "feat(watcher): events module — translates FS events to DB writes"
```

---

## Task 4: Watcher observer wrapper

**Files:**
- Create: `src/harbor_clerk/watcher/observer.py`
- Test: `tests/watcher/test_observer.py`

Wraps `watchdog.observers.Observer` with per-folder polling fallback. The observer translates `watchdog` events into `FileEvent` and hands them to a callback. Tests use synthetic events injected directly rather than real FS pokes (those land in `tests/watcher/test_main.py` later).

- [ ] **Step 1: Write the failing test**

```python
# tests/watcher/test_observer.py
import uuid
from pathlib import Path

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileDeletedEvent

from harbor_clerk.watcher.events import FileEvent, EventKind
from harbor_clerk.watcher.observer import FolderObserver


def test_translates_watchdog_create(tmp_path):
    captured = []
    folder_id = uuid.uuid4()
    obs = FolderObserver(folder_id, str(tmp_path), captured.append)
    obs._handler.on_created(FileCreatedEvent(str(tmp_path / "a.txt")))
    assert len(captured) == 1
    assert captured[0].kind == EventKind.created
    assert captured[0].relative_path == "a.txt"
    assert captured[0].folder_id == folder_id


def test_translates_watchdog_modify(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_modified(FileModifiedEvent(str(tmp_path / "sub" / "b.txt")))
    assert captured[0].kind == EventKind.modified
    assert captured[0].relative_path == "sub/b.txt"


def test_translates_watchdog_delete(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    obs._handler.on_deleted(FileDeletedEvent(str(tmp_path / "c.txt")))
    assert captured[0].kind == EventKind.deleted


def test_directory_events_ignored(tmp_path):
    captured = []
    obs = FolderObserver(uuid.uuid4(), str(tmp_path), captured.append)
    e = FileCreatedEvent(str(tmp_path / "subdir"))
    e.is_directory = True
    obs._handler.on_created(e)
    assert captured == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/watcher/test_observer.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write `observer.py`**

```python
# src/harbor_clerk/watcher/observer.py
"""Wraps watchdog.observers.Observer for one folder, with polling fallback."""

import logging
import os
import uuid
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from harbor_clerk.watcher.events import EventKind, FileEvent

logger = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, folder_id: uuid.UUID, root: str, sink: Callable[[FileEvent], None]):
        self.folder_id = folder_id
        self.root = root
        self.sink = sink

    def _emit(self, kind: EventKind, src_path: str) -> None:
        rel = os.path.relpath(src_path, self.root)
        if rel.startswith(".."):
            return
        self.sink(FileEvent(kind=kind, folder_id=self.folder_id, relative_path=rel, absolute_path=src_path))

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._emit(EventKind.created, event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._emit(EventKind.modified, event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._emit(EventKind.deleted, event.src_path)


class FolderObserver:
    """One observer per watched folder. Tries native first, falls back to polling."""

    def __init__(self, folder_id: uuid.UUID, root: str, sink: Callable[[FileEvent], None]):
        self.folder_id = folder_id
        self.root = root
        self._handler = _Handler(folder_id, root, sink)
        self._observer: Observer | PollingObserver | None = None

    def start(self) -> None:
        try:
            obs = Observer()
            obs.schedule(self._handler, self.root, recursive=True)
            obs.start()
            self._observer = obs
            logger.info("watcher: native observer started for %s", self.root)
        except Exception:
            logger.warning("watcher: native observer failed for %s, falling back to polling", self.root, exc_info=True)
            obs = PollingObserver()
            obs.schedule(self._handler, self.root, recursive=True)
            obs.start()
            self._observer = obs

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/watcher/test_observer.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/observer.py tests/watcher/test_observer.py
git commit -m "feat(watcher): observer wrapper with native+polling fallback"
```

---

## Task 5: Docker auto-discovery loop

**Files:**
- Create: `src/harbor_clerk/watcher/discovery.py`
- Test: `tests/watcher/test_discovery.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/watcher/test_discovery.py
from pathlib import Path

import pytest

from harbor_clerk.watcher.discovery import scan_watch_root
from harbor_clerk.models.watched import WatchedFolder


def test_new_subdir_creates_auto_discovered_folder(sync_session, tmp_path):
    (tmp_path / "inbox").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()
    rows = sync_session.query(WatchedFolder).all()
    assert len(rows) == 1
    assert rows[0].path == str(tmp_path / "inbox")
    assert rows[0].auto_discovered is True
    assert rows[0].display_name == "inbox"
    assert rows[0].unavailable_reason is None


def test_missing_subdir_marks_unavailable(sync_session, tmp_path):
    (tmp_path / "contracts").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    (tmp_path / "contracts").rmdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    row = sync_session.query(WatchedFolder).one()
    assert row.unavailable_reason == "unmounted"
    assert row.enabled is False


def test_remounted_subdir_clears_unavailable(sync_session, tmp_path):
    (tmp_path / "inbox").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    (tmp_path / "inbox").rmdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()
    (tmp_path / "inbox").mkdir()
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()

    row = sync_session.query(WatchedFolder).one()
    assert row.unavailable_reason is None
    assert row.enabled is True


def test_files_at_root_ignored(sync_session, tmp_path):
    (tmp_path / "loose.txt").write_text("hi")
    scan_watch_root(sync_session, str(tmp_path))
    sync_session.commit()
    assert sync_session.query(WatchedFolder).count() == 0


def test_empty_watch_root_string_is_noop(sync_session):
    scan_watch_root(sync_session, "")  # must not raise
    assert sync_session.query(WatchedFolder).count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/watcher/test_discovery.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write `discovery.py`**

```python
# src/harbor_clerk/watcher/discovery.py
"""Docker auto-discovery: every top-level subdir of WATCH_ROOT becomes a watched folder."""

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from harbor_clerk.models.watched import WatchedFolder

logger = logging.getLogger(__name__)


def scan_watch_root(session: Session, watch_root: str) -> None:
    """Reconcile watched_folders rows against the contents of WATCH_ROOT.

    - New subdir → insert row with auto_discovered=True.
    - Existing row whose path is gone → enabled=False, unavailable_reason='unmounted'.
    - Existing row whose path reappeared → clear unavailable_reason, enable.
    Caller is responsible for commit.
    """
    if not watch_root:
        return

    root = Path(watch_root)
    if not root.is_dir():
        logger.debug("watcher: WATCH_ROOT %s does not exist; skipping scan", watch_root)
        return

    on_disk = {str(p) for p in root.iterdir() if p.is_dir()}

    auto_rows = session.query(WatchedFolder).filter_by(auto_discovered=True).all()
    db_paths = {row.path for row in auto_rows}

    # New paths
    for path in on_disk - db_paths:
        session.add(
            WatchedFolder(
                path=path,
                bookmark_data=None,
                auto_discovered=True,
                display_name=Path(path).name,
            )
        )

    # Disappeared paths
    for row in auto_rows:
        if row.path in on_disk:
            if row.unavailable_reason == "unmounted":
                row.unavailable_reason = None
                row.enabled = True
        else:
            if row.unavailable_reason != "unmounted":
                row.unavailable_reason = "unmounted"
                row.enabled = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/watcher/test_discovery.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/discovery.py tests/watcher/test_discovery.py
git commit -m "feat(watcher): Docker WATCH_ROOT auto-discovery"
```

---

## Task 6: DB listener + notify helper

**Files:**
- Create: `src/harbor_clerk/watcher/db_listener.py`
- Create: `src/harbor_clerk/watcher/notify.py`
- Test: `tests/watcher/test_db_listener.py`

The watcher needs to react to folder add/remove/enable/disable from the API without restarting. Use Postgres `LISTEN watched_folders_changed`. The API fires `NOTIFY` via the helper module.

- [ ] **Step 1: Write the failing test**

```python
# tests/watcher/test_db_listener.py
import json
import time
import threading
import uuid

import pytest

from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.notify import notify_folder_change


def test_notify_received_by_listener(sync_session_factory):
    received: list[dict] = []
    stop = threading.Event()

    def loop():
        for payload in listen_for_folder_changes(sync_session_factory, stop_event=stop, poll_interval=0.05):
            received.append(payload)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    time.sleep(0.2)  # let LISTEN install

    folder_id = uuid.uuid4()
    sess = sync_session_factory()
    notify_folder_change(sess, folder_id, action="added")
    sess.commit()
    sess.close()

    deadline = time.time() + 2.0
    while not received and time.time() < deadline:
        time.sleep(0.05)

    stop.set()
    t.join(timeout=2.0)

    assert received and received[0]["folder_id"] == str(folder_id)
    assert received[0]["action"] == "added"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/watcher/test_db_listener.py -v`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Write `notify.py`**

```python
# src/harbor_clerk/watcher/notify.py
"""Helper for firing watched_folders_changed NOTIFY from API routes."""

import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

CHANNEL = "watched_folders_changed"


def notify_folder_change(session: Session, folder_id: uuid.UUID, action: str) -> None:
    """Fire NOTIFY watched_folders_changed with payload {folder_id, action}.

    `action` is one of: 'added', 'removed', 'enabled', 'disabled'.
    Caller is responsible for commit (NOTIFY is delivered on commit).
    """
    payload = json.dumps({"folder_id": str(folder_id), "action": action})
    session.execute(text("SELECT pg_notify(:ch, :p)"), {"ch": CHANNEL, "p": payload})
```

- [ ] **Step 4: Write `db_listener.py`**

```python
# src/harbor_clerk/watcher/db_listener.py
"""LISTEN watched_folders_changed — yields parsed payloads."""

import json
import logging
import threading
from typing import Iterator

from sqlalchemy.orm import sessionmaker

from harbor_clerk.watcher.notify import CHANNEL

logger = logging.getLogger(__name__)


def listen_for_folder_changes(
    session_factory: sessionmaker,
    stop_event: threading.Event,
    poll_interval: float = 1.0,
) -> Iterator[dict]:
    """Yield {folder_id, action} dicts as NOTIFYs arrive on watched_folders_changed."""
    session = session_factory()
    raw = session.connection().connection
    cursor = raw.cursor()
    cursor.execute(f"LISTEN {CHANNEL};")
    raw.commit()

    try:
        while not stop_event.is_set():
            raw.poll()
            while raw.notifies:
                n = raw.notifies.pop(0)
                try:
                    yield json.loads(n.payload)
                except Exception:
                    logger.warning("watcher: malformed NOTIFY payload: %r", n.payload)
            stop_event.wait(timeout=poll_interval)
    finally:
        try:
            cursor.execute(f"UNLISTEN {CHANNEL};")
            raw.commit()
        except Exception:
            pass
        session.close()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/watcher/test_db_listener.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/watcher/notify.py src/harbor_clerk/watcher/db_listener.py tests/watcher/test_db_listener.py
git commit -m "feat(watcher): LISTEN/NOTIFY for folder add/remove/enable/disable"
```

---

## Task 7: Watcher main entry point

**Files:**
- Create: `src/harbor_clerk/watcher/main.py`
- Modify: `pyproject.toml` (entry point + watchdog dep)
- Test: `tests/watcher/test_main.py`

Wires everything together. `main()` is the entry point. Picks up active folders from DB, starts a `FolderObserver` per folder, runs Docker auto-discovery loop in a background thread, runs DB listener loop in another thread to react to folder changes.

- [ ] **Step 1: Add `watchdog` dependency**

In `pyproject.toml`, add to the `[project] dependencies` list:

```toml
"watchdog>=4.0.0",
```

Run: `uv sync`
Expected: `watchdog` installed.

- [ ] **Step 2: Add entry point**

In `pyproject.toml`, in `[project.scripts]`:

```toml
harbor-clerk-watcher = "harbor_clerk.watcher.main:main"
```

Run: `uv sync`
Expected: `harbor-clerk-watcher` command available in `.venv/bin/`.

- [ ] **Step 3: Write the failing test**

```python
# tests/watcher/test_main.py
import time
import threading
import uuid
from pathlib import Path

import pytest

from harbor_clerk.watcher.main import WatcherDaemon
from harbor_clerk.models.watched import WatchedFolder, WatchedFile, WatchedFileStatus


def test_daemon_picks_up_existing_folder_and_ingests_new_files(sync_session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "")  # disable docker discovery
    sess = sync_session_factory()
    folder = WatchedFolder(path=str(tmp_path), display_name="t")
    sess.add(folder)
    sess.commit()
    sess.close()

    d = WatcherDaemon(sync_session_factory)
    d.start()
    try:
        time.sleep(0.5)  # let observer register
        (tmp_path / "doc.pdf").write_bytes(b"hello")
        deadline = time.time() + 5.0
        while time.time() < deadline:
            sess = sync_session_factory()
            wf = sess.query(WatchedFile).filter_by(folder_id=folder.folder_id).one_or_none()
            sess.close()
            if wf and wf.status == WatchedFileStatus.active:
                break
            time.sleep(0.1)
        assert wf is not None and wf.relative_path == "doc.pdf"
    finally:
        d.stop()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/watcher/test_main.py -v`
Expected: FAIL — module / class missing.

- [ ] **Step 5: Write `main.py`**

```python
# src/harbor_clerk/watcher/main.py
"""harbor-clerk-watcher entry point.

- Loads active watched_folders rows from DB.
- Starts a FolderObserver per folder.
- Runs Docker auto-discovery loop (no-op when WATCH_ROOT is empty).
- Listens for folder add/remove via PostgreSQL NOTIFY to update observer registrations live.
"""

import logging
import signal
import sys
import threading
import time
import uuid
from typing import Callable

from sqlalchemy.orm import sessionmaker

from harbor_clerk.config import get_settings
from harbor_clerk.db_sync import sync_engine
from harbor_clerk.log_setup import setup_logging
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.discovery import scan_watch_root
from harbor_clerk.watcher.events import FileEvent, handle_event
from harbor_clerk.watcher.observer import FolderObserver

logger = logging.getLogger(__name__)


class WatcherDaemon:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory
        self._observers: dict[uuid.UUID, FolderObserver] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _on_event(self, event: FileEvent) -> None:
        sess = self._session_factory()
        try:
            handle_event(sess, event)
            sess.commit()
        except Exception:
            sess.rollback()
            logger.exception("watcher: failed to handle event %r", event)
        finally:
            sess.close()

    def _sync_observers(self) -> None:
        sess = self._session_factory()
        try:
            rows = sess.query(WatchedFolder).filter_by(enabled=True).all()
            wanted = {row.folder_id: row.path for row in rows}
        finally:
            sess.close()

        # Stop observers no longer wanted
        for fid in list(self._observers):
            if fid not in wanted:
                self._observers[fid].stop()
                del self._observers[fid]
                logger.info("watcher: stopped observer for folder %s", fid)

        # Start new observers
        for fid, path in wanted.items():
            if fid not in self._observers:
                obs = FolderObserver(fid, path, self._on_event)
                obs.start()
                self._observers[fid] = obs

    def _discovery_loop(self) -> None:
        watch_root = get_settings().watch_root
        if not watch_root:
            return
        while not self._stop.is_set():
            sess = self._session_factory()
            try:
                scan_watch_root(sess, watch_root)
                sess.commit()
            except Exception:
                sess.rollback()
                logger.exception("watcher: discovery scan failed")
            finally:
                sess.close()
            self._sync_observers()
            self._stop.wait(timeout=60.0)

    def _listener_loop(self) -> None:
        for _payload in listen_for_folder_changes(self._session_factory, self._stop):
            self._sync_observers()

    def start(self) -> None:
        self._sync_observers()
        for target in (self._discovery_loop, self._listener_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for obs in self._observers.values():
            obs.stop()
        self._observers.clear()
        for t in self._threads:
            t.join(timeout=5.0)


def main() -> None:
    setup_logging()
    logger.info("harbor-clerk-watcher starting")
    factory = sessionmaker(bind=sync_engine, expire_on_commit=False)
    daemon = WatcherDaemon(factory)

    def _shutdown(signum, frame):
        logger.info("harbor-clerk-watcher: signal %s, shutting down", signum)
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    daemon.start()
    while True:
        time.sleep(60)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/watcher/test_main.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/harbor_clerk/watcher/main.py tests/watcher/test_main.py
git commit -m "feat(watcher): main entry point + watchdog dep"
```

---

## Task 8: API — `/api/watch/system`

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py`
- Test: `tests/api/test_watch_routes.py` (or wherever existing watch route tests live)

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_watch_routes.py — add or create
import platform

import pytest


@pytest.mark.asyncio
async def test_get_watch_system_macos(client_user, monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "")
    res = await client_user.get("/api/watch/system")
    assert res.status_code == 200
    body = res.json()
    assert body["picker"] in {"native", "none"}
    assert body["platform"] in {"macos", "docker"}
    assert "watch_root" in body


@pytest.mark.asyncio
async def test_get_watch_system_docker(client_user, monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "/data/watch")
    res = await client_user.get("/api/watch/system")
    assert res.status_code == 200
    body = res.json()
    assert body["platform"] == "docker"
    assert body["picker"] == "none"
    assert body["watch_root"] == "/data/watch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k system`
Expected: FAIL — endpoint missing.

- [ ] **Step 3: Add the endpoint**

In `src/harbor_clerk/api/routes/watch.py`, add:

```python
@router.get("/system")
async def get_system(
    principal: Principal = Depends(require_user),
):
    settings = get_settings()
    is_docker = bool(settings.watch_root)
    return {
        "platform": "docker" if is_docker else "macos",
        "picker": "none" if is_docker else "native",
        "watch_root": settings.watch_root or None,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k system`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py tests/api/test_watch_routes.py
git commit -m "feat(api): GET /api/watch/system"
```

---

## Task 9: API — `/api/watch/folders/{id}/progress`

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py`
- Test: `tests/api/test_watch_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_watch_routes.py — add
@pytest.mark.asyncio
async def test_folder_progress_aggregates(client_user, async_session, sample_folder_with_files):
    folder_id = sample_folder_with_files.folder_id
    res = await client_user.get(f"/api/watch/folders/{folder_id}/progress")
    assert res.status_code == 200
    body = res.json()
    assert body["total_files"] >= 1
    assert "by_stage" in body
    for stage in ("extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize"):
        assert stage in body["by_stage"]
        for state in ("pending", "running", "done", "error"):
            assert state in body["by_stage"][stage]
    assert body["scan_status"] in ("scanning", "idle")


@pytest.mark.asyncio
async def test_folder_progress_404_for_unknown(client_user):
    import uuid
    res = await client_user.get(f"/api/watch/folders/{uuid.uuid4()}/progress")
    assert res.status_code == 404
```

(Where `sample_folder_with_files` is a fixture you'll add in conftest that inserts a `WatchedFolder`, two `WatchedFile`s, and matching `IngestionJob` rows.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k progress`
Expected: FAIL — endpoint missing.

- [ ] **Step 3: Add the endpoint**

In `src/harbor_clerk/api/routes/watch.py`:

```python
@router.get("/folders/{folder_id}/progress")
async def get_folder_progress(
    folder_id: uuid.UUID,
    principal: Principal = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    folder = await session.get(WatchedFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")

    total = await session.scalar(
        select(func.count())
        .select_from(WatchedFile)
        .where(WatchedFile.folder_id == folder_id, WatchedFile.status == WatchedFileStatus.active)
    )

    # by_stage breakdown via aggregation join
    stages = ("extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize")
    by_stage: dict[str, dict[str, int]] = {s: {"pending": 0, "running": 0, "done": 0, "error": 0} for s in stages}

    rows = await session.execute(
        select(IngestionJob.stage, IngestionJob.status, func.count())
        .join(WatchedFile, WatchedFile.version_id == IngestionJob.version_id)
        .where(WatchedFile.folder_id == folder_id, WatchedFile.status == WatchedFileStatus.active)
        .group_by(IngestionJob.stage, IngestionJob.status)
    )
    for stage, status, count in rows:
        if stage in by_stage and status in by_stage[stage]:
            by_stage[stage][status] = count

    completed = by_stage["finalize"]["done"]

    return {
        "total_files": total or 0,
        "completed_files": completed,
        "by_stage": by_stage,
        "scan_status": "scanning" if folder.last_scan_at is None else "idle",
        "last_scan_at": folder.last_scan_at.isoformat() if folder.last_scan_at else None,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k progress`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py tests/api/test_watch_routes.py
git commit -m "feat(api): GET /api/watch/folders/{id}/progress"
```

---

## Task 10: API — POST validation + DELETE semantics + NOTIFY

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py`
- Test: `tests/api/test_watch_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_watch_routes.py — add
@pytest.mark.asyncio
async def test_post_folder_macos_rejects_nonexistent(client_admin, monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "")
    res = await client_admin.post("/api/watch/folders", json={"path": "/no/such/dir"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_post_folder_docker_rejects_outside_root(client_admin, monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    res = await client_admin.post("/api/watch/folders", json={"path": "/etc"})
    assert res.status_code == 400


@pytest.mark.asyncio
async def test_delete_active_auto_discovered_returns_409(client_admin, async_session, monkeypatch, tmp_path):
    monkeypatch.setenv("WATCH_ROOT", str(tmp_path))
    sub = tmp_path / "inbox"
    sub.mkdir()
    folder = WatchedFolder(path=str(sub), auto_discovered=True, display_name="inbox")
    async_session.add(folder)
    await async_session.commit()
    res = await client_admin.delete(f"/api/watch/folders/{folder.folder_id}")
    assert res.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k "post_folder or delete_active"`
Expected: FAIL.

- [ ] **Step 3: Tighten POST validation**

In `src/harbor_clerk/api/routes/watch.py`, replace the body of `POST /folders`:

```python
@router.post("/folders", status_code=201)
async def create_folder(
    body: FolderCreate,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()
    path = body.path

    if settings.watch_root:
        # Docker: must be a top-level subdir of WATCH_ROOT
        root = Path(settings.watch_root).resolve()
        candidate = Path(path).resolve()
        if candidate.parent != root or not candidate.is_dir():
            raise HTTPException(status_code=400, detail="Path must be a top-level subdir of WATCH_ROOT")
    else:
        # macOS: must exist + be readable + be a directory
        p = Path(path)
        if not p.is_dir() or not os.access(p, os.R_OK):
            raise HTTPException(status_code=400, detail="Path is not a readable directory")

    folder = WatchedFolder(
        path=path,
        display_name=body.display_name or Path(path).name,
        bookmark_data=None,
        auto_discovered=False,
    )
    session.add(folder)
    await session.commit()

    # Fire NOTIFY so the watcher picks up the new folder live
    sync_sess = get_sync_session()
    try:
        notify_folder_change(sync_sess, folder.folder_id, action="added")
        sync_sess.commit()
    finally:
        sync_sess.close()

    return folder
```

(Remove any pre-existing bookmark-data validation that was required before this migration.)

- [ ] **Step 4: Tighten DELETE semantics**

```python
@router.delete("/folders/{folder_id}", status_code=204)
async def delete_folder(
    folder_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    folder = await session.get(WatchedFolder, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    if folder.auto_discovered and folder.unavailable_reason is None:
        raise HTTPException(
            status_code=409,
            detail="Folder is an active Docker mount; unmount it first",
        )

    await session.delete(folder)
    await session.commit()

    sync_sess = get_sync_session()
    try:
        notify_folder_change(sync_sess, folder_id, action="removed")
        sync_sess.commit()
    finally:
        sync_sess.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_watch_routes.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py tests/api/test_watch_routes.py
git commit -m "feat(api): tighten POST validation, DELETE 409 for active Docker mounts, fire NOTIFY"
```

---

## Task 11: API — SSE `/api/watch/folders/stream`

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py`
- Test: `tests/api/test_watch_routes.py`

Implementation note: 1-second poll loop server-side (matches simplicity of existing `/api/jobs/stream`); aggregate per-folder progress and emit only deltas.

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_watch_routes.py — add
@pytest.mark.asyncio
async def test_folder_progress_stream_emits_initial_snapshot(client_user, async_session, sample_folder_with_files):
    async with client_user.stream("GET", "/api/watch/folders/stream") as res:
        assert res.status_code == 200
        async for line in res.aiter_lines():
            if line.startswith("data: "):
                import json
                payload = json.loads(line[6:])
                assert "folder_id" in payload
                assert "total_files" in payload
                assert "completed_files" in payload
                break
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k stream`
Expected: FAIL — endpoint missing.

- [ ] **Step 3: Add the endpoint**

This uses a 1-second polling loop inside the SSE generator (simpler than a new NOTIFY channel; aggregating two counts per folder is cheap). Each iteration acquires its own async session via `async_session_factory` directly so the generator's lifetime isn't tied to a single Depends-injected session.

In `src/harbor_clerk/api/routes/watch.py`:

```python
import asyncio
import json

from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from harbor_clerk.db import async_session_factory


@router.get("/folders/stream")
async def folder_progress_stream(
    principal: Principal = Depends(require_user),
):
    async def event_generator():
        prev: dict[str, tuple[int, int, str]] = {}
        try:
            while True:
                async with async_session_factory() as sess:
                    rows = (
                        await sess.execute(
                            select(
                                WatchedFolder.folder_id,
                                func.count(WatchedFile.file_id).filter(
                                    WatchedFile.status == WatchedFileStatus.active
                                ),
                                func.count(IngestionJob.job_id).filter(
                                    IngestionJob.stage == "finalize", IngestionJob.status == "done"
                                ),
                                WatchedFolder.last_scan_at,
                            )
                            .outerjoin(WatchedFile, WatchedFile.folder_id == WatchedFolder.folder_id)
                            .outerjoin(IngestionJob, IngestionJob.version_id == WatchedFile.version_id)
                            .group_by(WatchedFolder.folder_id, WatchedFolder.last_scan_at)
                        )
                    ).all()

                for fid, total, done, last_scan in rows:
                    scan_status = "scanning" if last_scan is None else "idle"
                    snap = (total or 0, done or 0, scan_status)
                    if prev.get(str(fid)) != snap:
                        prev[str(fid)] = snap
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "folder_id": str(fid),
                                    "total_files": snap[0],
                                    "completed_files": snap[1],
                                    "scan_status": snap[2],
                                }
                            )
                            + "\n\n"
                        )
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

If `async_session_factory` doesn't exist as importable from `harbor_clerk.db`, check `harbor_clerk/db.py` for the equivalent (e.g., `async_session_maker` or similar) and adjust the import. The existing `/api/jobs/stream` endpoint uses raw `asyncpg.connect` instead — that's the right pattern when listening on a NOTIFY channel, but for our polling approach the session-factory route is simpler.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_watch_routes.py -v -k stream`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py tests/api/test_watch_routes.py
git commit -m "feat(api): SSE /api/watch/folders/stream — debounced per-folder progress"
```

---

## Task 12: Docker compose — watcher service

**Files:**
- Modify: `docker-compose.yml`
- Modify: `docker/app.Dockerfile` (verify, don't duplicate)

- [ ] **Step 1: Add `watcher` service**

In `docker-compose.yml`, add:

```yaml
  watcher:
    build:
      context: .
      dockerfile: docker/app.Dockerfile
    command: harbor-clerk-watcher
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      WATCH_ROOT: /data/watch
      DATABASE_URL: ${DATABASE_URL}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    volumes:
      - ./data/watch:/data/watch
    restart: unless-stopped
```

- [ ] **Step 2: Verify the image includes `watchdog`**

Run: `docker compose build watcher && docker compose run --rm watcher harbor-clerk-watcher --help 2>&1 | head -5`
Expected: Watcher boots far enough to print logging or signal handler init.

- [ ] **Step 3: Smoke-test discovery**

Run:
```bash
mkdir -p ./data/watch/inbox
docker compose up -d postgres watcher
sleep 5
echo "hello" > ./data/watch/inbox/test.txt
sleep 5
docker compose exec postgres psql -U postgres -d harborclerk -c "SELECT path, auto_discovered FROM watched_folders;"
```
Expected: Row for `/data/watch/inbox` with `auto_discovered=t`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(docker): add watcher service"
```

---

## Task 13: Frontend — FoldersPage + nav swap

**Files:**
- Create: `frontend/src/pages/FoldersPage.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Create FoldersPage**

```tsx
// frontend/src/pages/FoldersPage.tsx
import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth'
import { del, get, post, patch } from '../api'

interface FolderInfo {
  folder_id: string
  path: string
  display_name: string
  enabled: boolean
  auto_discovered: boolean
  unavailable_reason: string | null
}

interface ProgressInfo {
  total_files: number
  completed_files: number
  by_stage: Record<string, { pending: number; running: number; done: number; error: number }>
  scan_status: 'scanning' | 'idle'
  last_scan_at: string | null
}

interface SystemInfo {
  platform: 'macos' | 'docker'
  picker: 'native' | 'none'
  watch_root: string | null
}

declare global {
  interface Window {
    harborclerk?: { pickFolder: () => Promise<string | null> }
  }
}

export default function FoldersPage() {
  const { token } = useAuth()
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [folders, setFolders] = useState<FolderInfo[]>([])
  const [progress, setProgress] = useState<Record<string, ProgressInfo>>({})
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState('')

  async function reload() {
    try {
      const sys = await get<SystemInfo>('/api/watch/system')
      setSystem(sys)
      const fs = await get<FolderInfo[]>('/api/watch/folders')
      setFolders(fs)
      const progs = await Promise.all(
        fs.map((f) => get<ProgressInfo>(`/api/watch/folders/${f.folder_id}/progress`)),
      )
      setProgress(Object.fromEntries(fs.map((f, i) => [f.folder_id, progs[i]])))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    }
  }

  useEffect(() => {
    reload()
  }, [])

  async function handleAdd() {
    if (system?.picker !== 'native' || !window.harborclerk) return
    const path = await window.harborclerk.pickFolder()
    if (!path) return
    try {
      await post('/api/watch/folders', { path })
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Add failed')
    }
  }

  async function handleDelete(folderId: string) {
    if (!confirm('Remove this folder? Documents already ingested will stay queryable.')) return
    try {
      await del(`/api/watch/folders/${folderId}`)
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  async function handleToggle(folder: FolderInfo) {
    try {
      await patch(`/api/watch/folders/${folder.folder_id}`, { enabled: !folder.enabled })
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Toggle failed')
    }
  }

  if (!system) return <div className="text-sm text-gray-500">Loading...</div>

  return (
    <div className="animate-slide-in">
      <h1 className="mb-4 text-xl font-bold">Folders</h1>

      {system.picker === 'native' ? (
        <button
          onClick={handleAdd}
          className="mb-4 rounded-lg bg-blue-600 px-3 py-1 text-xs font-medium text-white shadow-xs hover:bg-blue-700"
        >
          Add Folder
        </button>
      ) : (
        <div className="mb-4 rounded-md bg-blue-50 dark:bg-blue-900/20 p-3 text-xs text-blue-700 dark:text-blue-400">
          Folders are managed by mounting them under <code>{system.watch_root}</code> in your Docker setup.{' '}
          <a href="/docs/watched-folders-docker" className="underline">
            How to add a folder →
          </a>
        </div>
      )}

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="overflow-hidden rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border)">
        <table className="w-full text-sm">
          <thead className="bg-(--color-bg-secondary)">
            <tr>
              <th className="px-4 py-3 text-left">Folder</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Progress</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-(--color-border)">
            {folders.map((f) => {
              const p = progress[f.folder_id]
              const isExpanded = expanded === f.folder_id
              return (
                <>
                  <tr key={f.folder_id} className="cursor-pointer" onClick={() => setExpanded(isExpanded ? null : f.folder_id)}>
                    <td className="px-4 py-3">
                      <div className="font-medium">{f.display_name}</div>
                      <div className="text-xs text-gray-500 font-mono">{f.path}</div>
                      {f.auto_discovered && (
                        <span className="inline-flex items-center mt-1 rounded-md bg-purple-100 dark:bg-purple-900/30 px-2 py-0.5 text-[11px] text-purple-700 dark:text-purple-400">
                          auto-discovered
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {f.unavailable_reason === 'unmounted' ? (
                        <span className="rounded-md bg-red-100 dark:bg-red-900/30 px-2 py-0.5 text-[11px] text-red-700 dark:text-red-400">
                          unmounted
                        </span>
                      ) : !f.enabled ? (
                        <span className="rounded-md bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-[11px] text-gray-600 dark:text-gray-300">
                          disabled
                        </span>
                      ) : p?.scan_status === 'scanning' ? (
                        <span className="rounded-md bg-amber-100 dark:bg-amber-900/30 px-2 py-0.5 text-[11px] text-amber-700 dark:text-amber-400">
                          scanning
                        </span>
                      ) : (
                        <span className="rounded-md bg-green-100 dark:bg-green-900/30 px-2 py-0.5 text-[11px] text-green-700 dark:text-green-400">
                          idle
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs">
                      {p ? `${p.completed_files} / ${p.total_files}` : '—'}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={(e) => { e.stopPropagation(); handleToggle(f) }}
                        className="mr-2 rounded-lg border border-gray-400 px-2 py-1 text-xs"
                      >
                        {f.enabled ? 'Disable' : 'Enable'}
                      </button>
                      <button
                        disabled={f.auto_discovered && f.unavailable_reason === null}
                        title={f.auto_discovered && f.unavailable_reason === null ? 'Active Docker mount — unmount first' : ''}
                        onClick={(e) => { e.stopPropagation(); handleDelete(f.folder_id) }}
                        className="rounded-lg bg-red-600 px-2 py-1 text-xs text-white disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                  {isExpanded && p && (
                    <tr key={`${f.folder_id}-detail`}>
                      <td colSpan={4} className="bg-(--color-bg-secondary) px-4 py-3">
                        <div className="grid grid-cols-7 gap-2 text-xs">
                          {Object.entries(p.by_stage).map(([stage, counts]) => (
                            <div key={stage}>
                              <div className="font-medium capitalize">{stage}</div>
                              <div>{counts.done}/{counts.done + counts.pending + counts.running + counts.error}</div>
                              {counts.error > 0 && <div className="text-red-600">{counts.error} err</div>}
                            </div>
                          ))}
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Wire it into App.tsx**

In `frontend/src/App.tsx`:

1. Add the route:
```tsx
import FoldersPage from './pages/FoldersPage'
// ...
<Route path="/folders" element={<FoldersPage />} />
```

2. In the nav tab list, replace the existing Upload tab entry:
```tsx
// before:
<TabLink to="/upload">Upload</TabLink>
// after:
<TabLink to="/folders">Folders</TabLink>
```

The `/upload` route stays in the routes table — Stage 2 will remove it. Just remove the nav link.

- [ ] **Step 3: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: PASS (no errors).

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/FoldersPage.tsx frontend/src/App.tsx
git commit -m "feat(frontend): Folders page + nav swap"
```

---

## Task 14: Frontend — useFolderProgress hook (SSE)

**Files:**
- Create: `frontend/src/hooks/useFolderProgress.ts`
- Modify: `frontend/src/pages/FoldersPage.tsx`

- [ ] **Step 1: Create the hook**

```ts
// frontend/src/hooks/useFolderProgress.ts
import { useEffect, useRef } from 'react'
import { useAuth } from '../auth'

interface ProgressEvent {
  folder_id: string
  total_files: number
  completed_files: number
  scan_status: 'scanning' | 'idle'
}

export function useFolderProgress(onUpdate: (e: ProgressEvent) => void) {
  const { token } = useAuth()
  const onUpdateRef = useRef(onUpdate)
  onUpdateRef.current = onUpdate

  useEffect(() => {
    if (!token) return
    const controller = new AbortController()
    let reconnect: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      try {
        const res = await fetch('/api/watch/folders/stream', {
          headers: { Authorization: `Bearer ${token}` },
          signal: controller.signal,
        })
        if (!res.ok || !res.body) return
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buf = ''
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buf += decoder.decode(value, { stream: true })
          const lines = buf.split('\n')
          buf = lines.pop() || ''
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try {
                onUpdateRef.current(JSON.parse(line.slice(6)))
              } catch {
                /* ignore */
              }
            }
          }
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return
      }
      if (!controller.signal.aborted) reconnect = setTimeout(connect, 5000)
    }

    connect()
    return () => {
      controller.abort()
      if (reconnect) clearTimeout(reconnect)
    }
  }, [token])
}
```

- [ ] **Step 2: Wire into FoldersPage**

In `frontend/src/pages/FoldersPage.tsx`:

```tsx
import { useFolderProgress } from '../hooks/useFolderProgress'

// Inside the component, after `const [progress, setProgress] = ...`:
useFolderProgress((event) => {
  setProgress((prev) => ({
    ...prev,
    [event.folder_id]: prev[event.folder_id]
      ? { ...prev[event.folder_id], total_files: event.total_files, completed_files: event.completed_files, scan_status: event.scan_status }
      : prev[event.folder_id], // ignore for folders we don't have full progress for yet
  }))
})
```

- [ ] **Step 3: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/hooks/useFolderProgress.ts frontend/src/pages/FoldersPage.tsx
git commit -m "feat(frontend): live folder progress via SSE"
```

---

## Task 15: Frontend — Documents page folder column + filter

**Files:**
- Modify: `frontend/src/pages/DocumentsPage.tsx`
- Modify: `src/harbor_clerk/api/routes/docs.py` (or wherever the docs list endpoint lives) — add folder name to response

- [ ] **Step 1: Backend — include folder_name in docs response**

Find where `GET /api/docs` builds its response and join `WatchedFile → WatchedFolder.display_name` (LEFT JOIN — null for non-watched docs):

```python
# Pseudocode — adapt to actual route file
result = await session.execute(
    select(Document, WatchedFolder.display_name)
    .outerjoin(WatchedFile, WatchedFile.doc_id == Document.doc_id)
    .outerjoin(WatchedFolder, WatchedFolder.folder_id == WatchedFile.folder_id)
    # ... existing filters ...
)
# Include folder_name in each row's serialized form
```

Add a brief test that the new field is present (LEFT JOIN means null for direct-uploaded docs).

- [ ] **Step 2: Frontend — add column + filter chip**

In `frontend/src/pages/DocumentsPage.tsx`:

1. Extend the `Doc` type with `folder_name: string | null`.
2. Add a "Folder" `<th>` after the Title column.
3. Render `doc.folder_name ?? '—'` in each row.
4. Above the table, add a folder filter chip with options sourced from `/api/watch/folders`:

```tsx
const [folderFilter, setFolderFilter] = useState<string | 'all'>('all')
// ...
<select value={folderFilter} onChange={(e) => setFolderFilter(e.target.value)} className="...">
  <option value="all">All folders</option>
  {folders.map((f) => <option key={f.folder_id} value={f.display_name}>{f.display_name}</option>)}
</select>
```

5. Filter the rendered list client-side: `docs.filter(d => folderFilter === 'all' || d.folder_name === folderFilter)`.

- [ ] **Step 3: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check && cd .. && uv run pytest tests/ -k docs -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/DocumentsPage.tsx src/harbor_clerk/api/routes/docs.py tests/
git commit -m "feat(docs): folder column + client-side filter"
```

---

## Task 16: macOS — WKWebView pickFolder bridge

**Files:**
- Modify: `macos/HarborClerk/HarborClerk/HarborClerkApp.swift` (or the file containing the `WKWebView` setup; verify exact name)

- [ ] **Step 1: Locate the WebView setup**

Run: `grep -rn "WKWebView\|configuration" /Users/alex/mcp-gateway/macos/HarborClerk/HarborClerk/ | head -20`
Identify the file that constructs the `WKWebView`.

- [ ] **Step 2: Add the message handler**

In that file:

```swift
import AppKit
import WebKit

class FolderPickerHandler: NSObject, WKScriptMessageHandlerWithReply {
    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        DispatchQueue.main.async {
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.allowsMultipleSelection = false
            panel.canCreateDirectories = false
            if panel.runModal() == .OK, let url = panel.url {
                replyHandler(url.path, nil)
            } else {
                replyHandler(nil, nil)
            }
        }
    }
}

// During WKWebViewConfiguration setup:
let config = WKWebViewConfiguration()
let handler = FolderPickerHandler()
config.userContentController.addScriptMessageHandler(handler, contentWorld: .page, name: "pickFolder")
let initScript = WKUserScript(
    source: """
    window.harborclerk = window.harborclerk || {};
    window.harborclerk.pickFolder = () => window.webkit.messageHandlers.pickFolder.postMessage({});
    """,
    injectionTime: .atDocumentStart,
    forMainFrameOnly: true
)
config.userContentController.addUserScript(initScript)
```

- [ ] **Step 3: Manual smoke**

Build the macOS app (`cd macos && make apps`), launch it, navigate to `/folders`, click Add Folder, verify NSOpenPanel appears, pick a directory, verify the path lands in the API call (watch the network tab in Safari Web Inspector).

- [ ] **Step 4: Commit**

```bash
git add macos/HarborClerk/HarborClerk/
git commit -m "feat(macos): pickFolder JS bridge for web folder UI"
```

---

## Task 17: macOS — ServiceManager adds WatcherService

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift`
- Possibly create: `macos/HarborClerkServer/HarborClerkServer/WatcherService.swift`

- [ ] **Step 1: Create WatcherService**

Find an existing `Service.swift`-style file (e.g., the worker service) and follow that pattern. Create `WatcherService.swift`:

```swift
import Foundation

class WatcherService: BaseService {
    override var displayName: String { "Watcher" }
    override var executablePath: String { settings.venvDir.appendingPathComponent("bin/harbor-clerk-watcher").path }

    override func makeProcess() -> Process {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: executablePath)
        proc.environment = baseEnvironment()  // includes DATABASE_URL, NATIVE_CONFIG_FILE, etc.
        // No watch_root on macOS — empty env means non-Docker mode.
        return proc
    }
}
```

(Adapt `BaseService` reference to whatever the existing pattern is.)

- [ ] **Step 2: Wire into ServiceManager**

In `ServiceManager.swift`, add `WatcherService` to the service list, started after Postgres + workers, stopped before them.

- [ ] **Step 3: Manual smoke**

Run the menubar app, watch logs at `~/Library/Application Support/Harbor Clerk/logs/watcher.log`. Should see `harbor-clerk-watcher starting`. Add a folder via the web UI, drop a file, verify it ingests.

- [ ] **Step 4: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/
git commit -m "feat(macos): WatcherService managed subprocess"
```

---

## Task 18: macOS — retire WatchedFolderManager + Preferences section

**Files:**
- Delete: `macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift`
- Modify: `macos/HarborClerkServer/HarborClerkServer/PreferencesWindow.swift`
- Modify: `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` (remove old WFM references)
- Modify: `macos/HarborClerkServer/HarborClerkServer/HarborClerkServerApp.swift` (or wherever WFM is instantiated)

- [ ] **Step 1: Find all references**

Run: `grep -rn "WatchedFolderManager" /Users/alex/mcp-gateway/macos/`

- [ ] **Step 2: Delete the file**

```bash
rm macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift
```

- [ ] **Step 3: Remove the Preferences section**

In `PreferencesWindow.swift`, find the `WatchedFoldersSection` (or similar) view and delete it along with the tab/section that hosts it.

- [ ] **Step 4: Remove other references**

Delete every remaining reference found in Step 1 (instantiations, function calls, env-var setters that the new Python watcher doesn't need).

- [ ] **Step 5: Build to verify**

Run: `cd macos && make apps`
Expected: Build succeeds. Any `Cannot find 'WatchedFolderManager' in scope` errors mean there's a stale reference; fix and rebuild.

- [ ] **Step 6: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/
git commit -m "refactor(macos): retire WatchedFolderManager (Python watcher handles it now)"
```

---

## Task 19: Operator docs

**Files:**
- Create: `docs/watched-folders-docker.md`

- [ ] **Step 1: Write the doc**

```markdown
# Watched Folders on Docker

Harbor Clerk's Docker deployment ingests documents from watched folders that you mount into the `watcher` container. There is no "Add Folder" button in the web UI on Docker — folders appear automatically when you mount them.

## How it works

The `watcher` service watches `WATCH_ROOT` (default `/data/watch`) for new top-level subdirectories. Each subdirectory is registered as a watched folder. Files dropped into any subdirectory are ingested through the normal pipeline.

## Adding a folder

1. Create the directory on the host:
   ```bash
   mkdir -p ./data/watch/contracts
   ```
2. Edit `docker-compose.yml` (or your `compose.override.yml`) to mount it:
   ```yaml
   services:
     watcher:
       volumes:
         - ./data/watch:/data/watch
   ```
   The default `docker-compose.yml` already mounts `./data/watch` — you only need to add new subdirs on the host. No restart needed; the watcher rescans every 60 seconds.
3. Within ~60 seconds, the new folder appears at `/folders` in the web UI.

## Removing a folder

Remove the bind mount or delete the host directory. Within ~60 seconds the folder appears as `unmounted` in the web UI; you can then click Delete to remove the registry entry. Documents that were ingested from that folder remain in the corpus.

## Mounting non-local filesystems

NFS, SMB, fuse mounts, and similar are supported. The watcher detects when its native filesystem observer (inotify) fails to install on a given mount and automatically falls back to a 2-second polling loop for that folder. Polling is slower to detect changes but works on filesystems that don't deliver kernel events.

## Resource limits

The watcher process is single-threaded and lightweight. Even with thousands of files across many folders, the bottleneck is the ingestion pipeline (`worker-io` and `worker-cpu`) — not the watcher itself.
```

- [ ] **Step 2: Commit**

```bash
git add docs/watched-folders-docker.md
git commit -m "docs: operator guide for Docker watched folders"
```

---

## Task 20: Final verification

- [ ] **Step 1: Run full test suite**

```bash
uv run pytest tests/ -v
cd frontend && npm run lint && npm run type-check && npm run format:check
```
Expected: PASS across the board.

- [ ] **Step 2: Build macOS apps**

```bash
cd macos && make apps
```
Expected: BUILD SUCCEEDED for both apps.

- [ ] **Step 3: Manual smoke — macOS**

1. Launch HarborClerkServer.app.
2. Open the web app. Verify the nav bar shows "Folders" instead of "Upload".
3. Visit `/folders`. Click Add Folder, pick a tmp dir, verify it appears.
4. Drop a PDF and a DOCX into that dir. Within seconds, both should appear on `/docs` with the folder name populated.
5. Watch the progress bar tick up live (no manual refresh).
6. Disable the folder, drop another file, verify it does not get ingested. Re-enable, verify it catches up.
7. Delete the folder, verify the folder disappears from the UI but the documents stay.

- [ ] **Step 4: Manual smoke — Docker**

```bash
mkdir -p ./data/watch/contracts ./data/watch/invoices
docker compose up -d
sleep 5
# In web UI at /folders, both should appear with auto-discovered badge
echo "test" > ./data/watch/contracts/test.txt
sleep 10
# /docs should now show test.txt with folder=contracts
rm -rf ./data/watch/contracts
sleep 70  # wait one rescan cycle
# /folders should now show contracts as unmounted
```

- [ ] **Step 5: Polling fallback verification**

```bash
# On Linux, mount a tmpfs+fuse or NFS dir under WATCH_ROOT.
# Check watcher logs for: "watcher: native observer failed for /data/watch/<x>, falling back to polling"
docker compose logs watcher | grep -i "polling"
```

- [ ] **Step 6: Open PR**

```bash
git push -u origin <branch>
gh pr create --title "feat: watched-folder-first stage 1 — watcher unification + folder UI move" --body-file <(cat <<'EOF'
## Summary
- New cross-platform Python `harbor-clerk-watcher` daemon (uses `watchdog`, polling fallback).
- Schema migration `0016_watcher_unification`: `display_name`, `auto_discovered`, `unavailable_reason`; `bookmark_data` becomes nullable.
- New API endpoints: `GET /api/watch/system`, `GET /api/watch/folders/{id}/progress`, `GET /api/watch/folders/stream` (SSE).
- New top-level **Folders** tab in the web app (replaces Upload in nav). Per-folder progress with per-stage breakdown.
- Documents page gains folder column + client-side filter chip.
- macOS retires `WatchedFolderManager.swift` and the menubar Preferences section. `WKWebView` now exposes `harborclerk.pickFolder()`.
- Docker compose adds a `watcher` service. `WATCH_ROOT` env var (default `/data/watch`); top-level subdirs auto-discovered.
- Operator docs at `docs/watched-folders-docker.md`.

Spec: `docs/superpowers/specs/2026-04-30-watched-folder-first-stage-1-design.md`

## Test plan
- [x] Python tests pass (watcher unit + API integration).
- [x] Frontend lint + typecheck.
- [x] macOS build succeeds.
- [x] Manual smoke on macOS: add folder, drop file, observe ingestion + live progress.
- [x] Manual smoke on Docker: mount → auto-discover, unmount → unmounted pill.
- [x] Polling fallback exercised on a non-inotify filesystem.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```

- [ ] **Step 7: Watch CI, fix any failures, merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```
