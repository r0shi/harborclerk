# Phase 5: Skipped-File Hygiene — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track per-folder counts and extension fingerprints of files the watcher skipped for unsupported extensions, expose them via the API, and surface them in the Folders UI so users can see — at a glance — *"3 files not ingested — unsupported types: .canvas, .excalidraw"*.

**Architecture:** Two new columns on `watched_folders` (`skipped_count: int`, `skipped_extensions: list[str]`). The watcher's initial-scan path (`_scan_folder`) classifies each rejected file as either "noise" (dotfiles, AppleDouble, `__MACOSX`, Excalidraw) or "unsupported extension"; only the latter counts. Counts are aggregated per-scan and written to the folder row when the scan completes. The watch API serializer returns the new fields; `FoldersPage.tsx` shows a small muted line below the folder when `skipped_count > 0`.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0, alembic (PG `ARRAY(Text)`), pytest, React/TypeScript. No new third-party dependencies.

**Working directory:** the `feat/markdown-text-handling` worktree. All paths below are relative to its root. Phase 4 must be committed before starting.

**Spec:** `docs/superpowers/specs/2026-05-22-markdown-text-handling-design.md` (Phase 5, lines 210–228).

**Spec deviation:** the spec mentions "migration `0002`" for this phase, but Phase 4 used `0002_document_links`, so this phase ships as `0003_watched_folder_skip_tracking`. The functional behaviour is unchanged from the spec — only the migration filename / revision id differ.

---

## File Structure

- **Modify** `src/harbor_clerk/models/watched.py` — add `skipped_count: int` and `skipped_extensions: list[str]` columns to `WatchedFolder`.
- **Create** `alembic/versions/0003_watched_folder_skip_tracking.py` — migration adding the two columns with safe defaults.
- **Modify** `src/harbor_clerk/watcher/events.py` — add a `SkipReason` enum + `classify_skip(relative_path) -> SkipReason | None` helper. Keep `_should_ignore` as a thin compatibility wrapper so other callers don't break.
- **Modify** `src/harbor_clerk/watcher/main.py` — `_scan_folder` builds a `(count, extensions_set)` tally during the walk, then writes `skipped_count` + `skipped_extensions` (sorted, distinct) to the folder row alongside `last_scan_at`.
- **Modify** `src/harbor_clerk/api/routes/watch.py` — `_folder_to_dict` returns the two new fields.
- **Modify** `frontend/src/pages/FoldersPage.tsx` — add `skipped_count` + `skipped_extensions` to the `FolderInfo` interface and render the muted "N files not ingested — unsupported types: …" line.
- **Modify** `tests/watcher/test_events.py` — unit tests for `classify_skip`.
- **Modify** `tests/watcher/test_main.py` (or add a new test file under `tests/watcher/`) — integration test verifying `_scan_folder` writes the tally to the row.
- **Modify** `tests/api/` (whichever file covers `/watch/folders`) or add a new one — endpoint test that the serializer returns the new fields.

Build order: Task 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8. The migration (Task 1) must run before any DB-backed test in later tasks.

---

### Task 1: Schema — columns + migration `0003`

**Files:**
- Modify: `src/harbor_clerk/models/watched.py`
- Create: `alembic/versions/0003_watched_folder_skip_tracking.py`
- Create: `tests/test_watched_folder_skip_schema.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_watched_folder_skip_schema.py`:

```python
"""Schema test: WatchedFolder gains skipped_count + skipped_extensions."""

import uuid

from sqlalchemy import select

from harbor_clerk.models.watched import WatchedFolder


def test_watched_folder_skip_columns_defaults(sync_session):
    """A folder inserted without the new fields gets skipped_count=0 and
    skipped_extensions=[] from the column defaults."""
    folder = WatchedFolder(path="/tmp/test-skip-defaults")
    sync_session.add(folder)
    sync_session.commit()

    row = sync_session.execute(
        select(WatchedFolder).where(WatchedFolder.folder_id == folder.folder_id)
    ).scalar_one()
    assert row.skipped_count == 0
    assert row.skipped_extensions == []


def test_watched_folder_skip_columns_round_trip(sync_session):
    """The fields can be written and read back as int + list[str]."""
    folder = WatchedFolder(
        path="/tmp/test-skip-roundtrip",
        skipped_count=7,
        skipped_extensions=[".canvas", ".excalidraw", ".xyz"],
    )
    sync_session.add(folder)
    sync_session.commit()

    row = sync_session.execute(
        select(WatchedFolder).where(WatchedFolder.folder_id == folder.folder_id)
    ).scalar_one()
    assert row.skipped_count == 7
    # PG ARRAY(Text) returns a list, not a set — order is preserved as inserted.
    assert row.skipped_extensions == [".canvas", ".excalidraw", ".xyz"]
```

The `sync_session` fixture: this file follows the same pattern as `tests/test_document_link_model.py` (Phase 4) — define a per-test fixture inside the file that creates a psycopg2-backed session and truncates `watched_folders` (and any FK-dependent tables) on teardown. Copy the fixture body from `tests/test_document_link_model.py`, but adjust the teardown table list to include only the watched tables (`watched_files`, `watched_folders`) since this test doesn't touch documents.

If, after writing this file, you find that another test file already provides a reusable `sync_session` fixture via `conftest.py`, prefer that. As of Phase 4 there was none and we duplicated the fixture per-file — that's the current pattern.

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_watched_folder_skip_schema.py -v`
Expected: `AttributeError: 'WatchedFolder' object has no attribute 'skipped_count'` (or similar).

- [ ] **Step 3: Update the model**

In `src/harbor_clerk/models/watched.py`, extend the imports and add the two columns to `WatchedFolder`.

Add to the existing SQLAlchemy import line:

```python
from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, Text, text
```

(adds `Integer` and `text`).

Add this import:

```python
from sqlalchemy.dialects.postgresql import ARRAY, UUID
```

(adds `ARRAY` to the existing postgresql import).

Inside the `WatchedFolder` class, AFTER the `auto_discovered` column and BEFORE the `created_at` column, add:

```python
    # Phase 5: skipped-file hygiene. Updated by the watcher's initial scan.
    # ``skipped_count`` is the number of files in the folder whose extension
    # was unsupported (dotfiles / AppleDouble / __MACOSX / Excalidraw are
    # excluded — they're noise, not "files the user might expect ingested").
    # ``skipped_extensions`` is the sorted distinct list of those extensions
    # (lowercase, with leading dot, e.g. ``[".canvas", ".excalidraw"]``).
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    skipped_extensions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
```

- [ ] **Step 4: Create the migration**

Create `alembic/versions/0003_watched_folder_skip_tracking.py`:

```python
"""watched_folder skip tracking

Adds skipped_count + skipped_extensions to watched_folders. Phase 5 of the
markdown-handling feature: the watcher counts files it rejected for an
unsupported extension during the initial scan, so the UI can surface
"N files not ingested — unsupported types: .canvas, .excalidraw" per folder.

Revision ID: 0003_watched_folder_skip_tracking
Revises: 0002_document_links
Create Date: 2026-05-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_watched_folder_skip_tracking"
down_revision: str | None = "0002_document_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "watched_folders",
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "watched_folders",
        sa.Column(
            "skipped_extensions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )


def downgrade() -> None:
    op.drop_column("watched_folders", "skipped_extensions")
    op.drop_column("watched_folders", "skipped_count")
```

- [ ] **Step 5: Run the test, verify it passes**

The test fixture in `tests/conftest.py` runs `alembic upgrade head` against the test DB before tests, so the migration applies automatically on first test run.

Run: `uv run pytest tests/test_watched_folder_skip_schema.py -v`
Expected: PASS (2 tests).

Run: `uv run ruff check src/harbor_clerk/models/watched.py alembic/versions/0003_watched_folder_skip_tracking.py tests/test_watched_folder_skip_schema.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/models/watched.py alembic/versions/0003_watched_folder_skip_tracking.py tests/test_watched_folder_skip_schema.py
git commit -m "feat(watch): watched_folders.skipped_count + skipped_extensions columns"
```

---

### Task 2: `classify_skip` helper in `watcher/events.py`

A small helper that classifies a path that `_should_ignore` would reject into either `SkipReason.NOISE` (dotfiles, AppleDouble, `__MACOSX`, Excalidraw — intentional filters; not surfaced to user) or `SkipReason.UNSUPPORTED_EXTENSION` (a real file the user might have expected ingested). Returns `None` for paths that would NOT be ignored.

**Files:**
- Modify: `src/harbor_clerk/watcher/events.py`
- Modify: `tests/watcher/test_events.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/watcher/test_events.py`:

```python
# --- classify_skip tests ---


from harbor_clerk.watcher.events import SkipReason, classify_skip


class TestClassifySkip:
    def test_returns_none_for_allowed_file(self):
        assert classify_skip("contract.pdf") is None
        assert classify_skip("notes.md") is None
        assert classify_skip("subdir/photo.jpg") is None
        assert classify_skip("Spreadsheet.XLSX") is None  # case insensitive

    def test_noise_dotfile(self):
        assert classify_skip(".gitignore") is SkipReason.NOISE
        assert classify_skip(".git/HEAD") is SkipReason.NOISE
        assert classify_skip(".DS_Store") is SkipReason.NOISE
        assert classify_skip("subdir/.DS_Store") is SkipReason.NOISE

    def test_noise_apple_double(self):
        assert classify_skip("._Document.pdf") is SkipReason.NOISE
        assert classify_skip("subfolder/._Document.pdf") is SkipReason.NOISE

    def test_noise_macosx_dir(self):
        assert classify_skip("__MACOSX/foo.pdf") is SkipReason.NOISE

    def test_noise_excalidraw(self):
        assert classify_skip("diagram.excalidraw.md") is SkipReason.NOISE
        assert classify_skip("subdir/whiteboard.excalidraw.md") is SkipReason.NOISE

    def test_unsupported_extension(self):
        assert classify_skip("malware.exe") is SkipReason.UNSUPPORTED_EXTENSION
        assert classify_skip("library.dll") is SkipReason.UNSUPPORTED_EXTENSION
        assert classify_skip("vimswap.swp") is SkipReason.UNSUPPORTED_EXTENSION
        assert classify_skip("file.canvas") is SkipReason.UNSUPPORTED_EXTENSION

    def test_no_extension_is_unsupported(self):
        """README, LICENSE — these are files the user might reasonably expect
        ingested. Counting them as unsupported (not noise) gives a more useful
        UX prompt than silently filtering."""
        assert classify_skip("README") is SkipReason.UNSUPPORTED_EXTENSION
        assert classify_skip("LICENSE") is SkipReason.UNSUPPORTED_EXTENSION
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/watcher/test_events.py::TestClassifySkip -v`
Expected: `ImportError: cannot import name 'SkipReason'` (and `classify_skip`).

- [ ] **Step 3: Implement `SkipReason` + `classify_skip`**

In `src/harbor_clerk/watcher/events.py`, add to the `Enum` import — it already imports `Enum`. After the existing `EventKind` enum (around line 39–42), add:

```python
class SkipReason(str, Enum):
    """Why a path was skipped by the watcher.

    ``NOISE`` covers intentional, hard-coded filters that the user never
    expects to ingest (dotfiles, AppleDouble shadow files, ``__MACOSX``
    archive metadata, Excalidraw notes). The UI does not surface these.

    ``UNSUPPORTED_EXTENSION`` covers real files whose extension isn't in
    the allowlist. These ARE surfaced per-folder ("N files not ingested —
    unsupported types: …") so the user can decide whether to extend the
    allowlist or accept the omission.
    """

    NOISE = "noise"
    UNSUPPORTED_EXTENSION = "unsupported_extension"
```

Then add the `classify_skip` helper, immediately AFTER `_should_ignore` (so `_should_ignore` can be expressed in terms of it):

```python
def classify_skip(relative_path: str) -> SkipReason | None:
    """Classify why ``relative_path`` would be skipped by the watcher.

    Returns ``None`` if the path WOULD be accepted by ``_should_ignore``
    (i.e. nothing to skip). Otherwise returns the reason, splitting
    intentional-noise filters from real "unsupported extension" rejects
    so callers can count only the latter.
    """
    parts = relative_path.split("/")
    if any(p == "__MACOSX" for p in parts):
        return SkipReason.NOISE
    if any(p.startswith("._") for p in parts):
        return SkipReason.NOISE
    if any(p.startswith(".") and p not in ("", ".", "..") for p in parts):
        return SkipReason.NOISE
    if is_excalidraw(relative_path):
        return SkipReason.NOISE
    suffix = Path(relative_path).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return SkipReason.UNSUPPORTED_EXTENSION
    return None
```

Replace `_should_ignore` with a thin wrapper so the existing callers continue to work and the filter logic doesn't duplicate:

```python
def _should_ignore(relative_path: str) -> bool:
    """True if the path should be filtered out (noise OR unsupported extension).

    Kept as a thin wrapper around ``classify_skip`` so callers that don't
    care about the reason (most of them) stay unchanged.
    """
    return classify_skip(relative_path) is not None
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/watcher/test_events.py -v`
Expected: PASS (all prior tests still pass — the wrapper preserves `_should_ignore` semantics — plus the new `TestClassifySkip` class).

Run: `uv run ruff check src/harbor_clerk/watcher/events.py tests/watcher/test_events.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/events.py tests/watcher/test_events.py
git commit -m "feat(watcher): classify_skip helper for counting unsupported skips"
```

---

### Task 3: `_scan_folder` aggregates and writes per-folder skip counts

The initial-scan path tallies UNSUPPORTED_EXTENSION skips during the walk and writes the totals to the folder row when the scan completes. NOISE skips are excluded — they're intentional filters, not "files the user might expect ingested".

**Files:**
- Modify: `src/harbor_clerk/watcher/main.py`
- Modify: `tests/watcher/test_main.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/watcher/test_main.py`:

```python
def test_scan_folder_writes_skipped_count_and_extensions(factory, tmp_path, monkeypatch):
    """After _scan_folder visits a folder containing unsupported files,
    watched_folders.skipped_count and .skipped_extensions are updated.
    Dotfiles, AppleDouble, __MACOSX, and *.excalidraw.md are NOT counted."""
    import uuid
    from sqlalchemy import select

    from harbor_clerk.models.watched import WatchedFolder
    from harbor_clerk.watcher.main import WatcherDaemon

    # Layout:
    #   doc.pdf            → accepted
    #   notes.md           → accepted
    #   malware.exe        → counted (unsupported)
    #   board.canvas       → counted (unsupported)
    #   diagram.excalidraw.md → NOT counted (noise: excalidraw)
    #   .DS_Store          → NOT counted (noise: dotfile)
    #   ._shadow.pdf       → NOT counted (noise: AppleDouble)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%fake pdf\n")
    (tmp_path / "notes.md").write_text("# notes\n")
    (tmp_path / "malware.exe").write_bytes(b"MZ")
    (tmp_path / "board.canvas").write_text("{}\n")
    (tmp_path / "diagram.excalidraw.md").write_text("---\nexcalidraw-plugin: parsed\n---\n")
    (tmp_path / ".DS_Store").write_bytes(b"")
    (tmp_path / "._shadow.pdf").write_bytes(b"")

    # Create the folder row first.
    sess = factory()
    folder = WatchedFolder(path=str(tmp_path), auto_discovered=False)
    sess.add(folder)
    sess.commit()
    folder_id = folder.folder_id
    sess.close()

    daemon = WatcherDaemon(factory)
    # Avoid spinning up real observers — only the scan logic is under test.
    daemon._scan_folder(folder_id, str(tmp_path))

    sess = factory()
    try:
        row = sess.execute(select(WatchedFolder).where(WatchedFolder.folder_id == folder_id)).scalar_one()
        assert row.skipped_count == 2, (
            f"expected 2 (malware.exe + board.canvas), got {row.skipped_count}; "
            f"extensions={row.skipped_extensions!r}"
        )
        # Lowercased, sorted, distinct.
        assert row.skipped_extensions == [".canvas", ".exe"]
        # Scan completion marker also set.
        assert row.last_scan_at is not None
    finally:
        sess.close()
```

If `factory` fixture / `WatcherDaemon` test fixture isn't already set up the way the existing `test_main.py` tests use them, follow the existing patterns in that file (read it first). The test should fit alongside `test_daemon_scans_existing_files_when_observer_registers` which is the existing test of the same scan path.

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/watcher/test_main.py::test_scan_folder_writes_skipped_count_and_extensions -v`
Expected: FAIL — `skipped_count` is still 0 because `_scan_folder` doesn't track it yet.

- [ ] **Step 3: Update `_scan_folder` to track + write the tally**

In `src/harbor_clerk/watcher/main.py`, update the existing imports — add `classify_skip` and `SkipReason`:

```python
from harbor_clerk.watcher.events import (
    EventKind,
    FileEvent,
    SkipReason,
    classify_skip,
)
```

(Add `SkipReason` and `classify_skip` to whatever the existing `from harbor_clerk.watcher.events import …` line is. The plan's named import list assumes EventKind+FileEvent were already there; adjust to match what's actually in the file.)

Update `_scan_folder` (around lines 97–145). The walk loop becomes:

```python
    def _scan_folder(self, folder_id: uuid.UUID, root: str) -> None:
        """Walk an existing folder and emit synthetic `created` events for
        every file already on disk. Required because watchdog only delivers
        events for changes that happen AFTER the observer starts — files
        already in the folder when it was added would never be ingested.

        Updates `watched_folders.last_scan_at` when done so the API's
        scan_status flips from "scanning" to "idle". Also tallies the
        per-folder ``skipped_count`` + ``skipped_extensions`` from files
        the watcher rejected for an unsupported extension (NOT counting
        intentional-noise filters: dotfiles, AppleDouble, __MACOSX,
        Excalidraw).
        """
        logger.info("watcher: initial scan of %s starting", root)
        count = 0
        skipped_count = 0
        skipped_exts: set[str] = set()
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune dotfile / __MACOSX directories so we don't recurse
                # into them — both for speed and to avoid the cost of
                # invoking _on_event for every file in a .git tree.
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
                for fname in filenames:
                    abs_path = os.path.join(dirpath, fname)
                    rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")

                    # Classify BEFORE handing off to _on_event so we can
                    # tally UNSUPPORTED_EXTENSION skips without going
                    # through the event-handler path twice.
                    reason = classify_skip(rel_path)
                    if reason is SkipReason.UNSUPPORTED_EXTENSION:
                        skipped_count += 1
                        suffix = os.path.splitext(rel_path)[1].lower()
                        if suffix:
                            skipped_exts.add(suffix)
                        # Don't dispatch the event — handle_event would
                        # ignore it anyway, but skipping the call saves
                        # the SHA computation and DB roundtrip.
                        continue
                    if reason is SkipReason.NOISE:
                        # Noise filters are intentional; don't tally and
                        # don't dispatch.
                        continue

                    self._on_event(
                        FileEvent(
                            kind=EventKind.created,
                            folder_id=folder_id,
                            relative_path=rel_path,
                            absolute_path=abs_path,
                        )
                    )
                    count += 1
                    if self._stop.is_set():
                        return
        except Exception:
            logger.exception("watcher: initial scan of %s failed", root)
            return

        # Mark scan complete and write the skip tally on the folder row.
        sess = self._session_factory()
        try:
            folder = sess.query(WatchedFolder).filter_by(folder_id=folder_id).one_or_none()
            if folder is not None:
                folder.last_scan_at = datetime.now(UTC)
                folder.skipped_count = skipped_count
                folder.skipped_extensions = sorted(skipped_exts)
                sess.commit()
        except Exception:
            sess.rollback()
            logger.exception("watcher: failed to update last_scan_at for %s", folder_id)
        finally:
            sess.close()

        logger.info(
            "watcher: initial scan of %s complete (%d files visited, %d skipped — extensions=%s)",
            root,
            count,
            skipped_count,
            sorted(skipped_exts),
        )
```

The two changes from the existing function:
1. Inside the inner `for fname` loop, classify the path up front. UNSUPPORTED_EXTENSION → tally + `continue`. NOISE → `continue` without tallying. Anything else → existing `_on_event` dispatch.
2. The trailing folder-row update writes `skipped_count` + `skipped_extensions` (sorted list) alongside `last_scan_at`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/watcher/ -v`
Expected: all PASS. (The new test plus all pre-existing watcher tests should still pass — `_scan_folder` still calls `_on_event` for accepted files, and `_should_ignore` still has the same semantics via the `classify_skip` wrapper.)

Run: `uv run ruff check src/harbor_clerk/watcher/main.py tests/watcher/test_main.py` — clean.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/main.py tests/watcher/test_main.py
git commit -m "feat(watcher): tally skipped_count + skipped_extensions in _scan_folder"
```

---

### Task 4: API serializer returns the new fields

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py`
- Modify: a test file under `tests/` that already covers `/watch/folders` (probably `tests/test_api_watch.py` or `tests/api/test_watch.py` — find it first; if none, append to `tests/watcher/test_main.py` or create `tests/test_api_watch_skip.py`).

- [ ] **Step 1: Locate the existing endpoint test for `/watch/folders`**

Run:

```bash
grep -rn "/watch/folders\|list_folders\b" tests/ | head -20
```

Pick the test file whose patterns match `client.get("/api/watch/folders")`. Append the new test there. If no such test exists, create `tests/api/test_watch_skip.py` and follow the patterns from `tests/api/` for setting up an authenticated client.

- [ ] **Step 2: Write the failing test**

Append (or create) — adjust the fixture names to match whatever the existing file uses (`client`, `admin_user`, `db_session`, etc.):

```python
@pytest.mark.asyncio
async def test_folder_serializer_returns_skip_fields(client, db_session, admin_user):
    """GET /api/watch/folders returns skipped_count + skipped_extensions per folder."""
    import json

    from harbor_clerk.models.watched import WatchedFolder

    folder = WatchedFolder(
        path="/tmp/test-skip-serializer",
        skipped_count=4,
        skipped_extensions=[".canvas", ".exe", ".tmp"],
    )
    db_session.add(folder)
    await db_session.commit()

    # Use whatever auth pattern the file already uses — admin_user header,
    # cookie, fixture-injected, etc.
    resp = await client.get("/api/watch/folders")
    assert resp.status_code == 200
    rows = resp.json()
    by_path = {r["path"]: r for r in rows}
    assert "/tmp/test-skip-serializer" in by_path
    row = by_path["/tmp/test-skip-serializer"]
    assert row["skipped_count"] == 4
    assert row["skipped_extensions"] == [".canvas", ".exe", ".tmp"]
```

- [ ] **Step 3: Run the test, verify it fails**

Run with the exact test path from step 2.
Expected: `KeyError: 'skipped_count'` — the serializer doesn't return them yet.

- [ ] **Step 4: Extend `_folder_to_dict`**

In `src/harbor_clerk/api/routes/watch.py`, update `_folder_to_dict`:

```python
def _folder_to_dict(f: WatchedFolder, file_count: int) -> dict:
    return {
        "folder_id": str(f.folder_id),
        "path": f.path,
        "recursive": f.recursive,
        "enabled": f.enabled,
        "last_event_id": f.last_event_id,
        "last_scan_at": f.last_scan_at.isoformat() if f.last_scan_at else None,
        "file_count": file_count,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "display_name": f.display_name,
        "auto_discovered": f.auto_discovered,
        "unavailable_reason": f.unavailable_reason,
        "skipped_count": f.skipped_count,
        "skipped_extensions": list(f.skipped_extensions or []),
    }
```

The `or []` is defensive — `ARRAY` shouldn't return `None` once `server_default` is set, but it costs nothing to be safe. `list(...)` ensures the JSON serializer sees a list, not a tuple or other sequence type SQLAlchemy might happen to use.

- [ ] **Step 5: Run the test, verify it passes**

Expected: PASS.

Run: `uv run ruff check src/harbor_clerk/api/routes/watch.py <test-file>` — clean.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py <test-file>
git commit -m "feat(api): include skipped_count + skipped_extensions in folder serializer"
```

---

### Task 5: Folders UI shows the muted skip line

**Files:**
- Modify: `frontend/src/pages/FoldersPage.tsx`

- [ ] **Step 1: Extend the `FolderInfo` interface**

In `frontend/src/pages/FoldersPage.tsx`, find the existing `interface FolderInfo` (around lines 11–18). Add the two new fields:

```typescript
interface FolderInfo {
  folder_id: string
  path: string
  display_name: string | null
  enabled: boolean
  auto_discovered: boolean
  unavailable_reason: string | null
  skipped_count: number
  skipped_extensions: string[]
}
```

- [ ] **Step 2: Add the helper that formats the skip message**

Above the `FoldersPage` component (so it's reusable and unit-testable), add a small pure helper:

```typescript
function formatSkipMessage(count: number, exts: string[]): string | null {
  if (count <= 0) return null
  const noun = count === 1 ? 'file' : 'files'
  if (exts.length === 0) {
    return `${count} ${noun} not ingested — unsupported type`
  }
  // Truncate long lists for the muted single-line affordance.
  const MAX = 6
  const shown = exts.slice(0, MAX).join(', ')
  const overflow = exts.length > MAX ? ` and ${exts.length - MAX} more` : ''
  return `${count} ${noun} not ingested — unsupported types: ${shown}${overflow}`
}
```

- [ ] **Step 3: Render the message under each folder**

Find the folder-row JSX in the `FoldersPage` component. There should be a section that renders `path`, `display_name`, the `StatusPill`, etc. for each folder. Below the existing folder metadata (or below the path / status row, wherever feels least intrusive), add the muted line:

```tsx
{(() => {
  const msg = formatSkipMessage(f.skipped_count, f.skipped_extensions)
  if (!msg) return null
  return (
    <div
      className="text-xs mt-1"
      style={{ color: 'var(--text-muted)' }}
      title={f.skipped_extensions.join(', ')}
    >
      {msg}
    </div>
  )
})()}
```

If `--text-muted` isn't the right CSS variable in this theme (it should be — check `frontend/src/index.css`), substitute whatever the existing pages use for muted secondary text.

If you're uncertain where exactly to drop the message in the JSX (because the layout has multiple candidate locations), read the surrounding rendering code for ~30 lines around the `mapFolderStatusLabel` usage and pick the spot that:
- is per-folder (inside the map callback that iterates folders),
- is the deepest text-only sibling of the status pill / last-scan timestamp,
- is rendered for both enabled and disabled folders (the count is informational either way).

If you're STILL uncertain after reading the JSX, STOP and report — the location matters for usability but the *information* is the same regardless.

- [ ] **Step 4: Type-check + lint**

Run:

```bash
cd frontend
npm run lint
npm run type-check
npm run format:check
```

All must pass.

If the codebase has frontend unit tests (check `frontend/src/` for `*.test.ts(x)`), prefer adding one for `formatSkipMessage` covering: zero count → null, count=1 → "1 file", count=N → "N files", empty exts list → "unsupported type" (no list), exts.length > MAX → "… and K more". If no frontend test setup exists, skip the unit test and trust the visual review.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/FoldersPage.tsx
git commit -m "feat(ui): surface per-folder skipped-file counts in Folders page"
```

---

### Task 6: End-to-end watcher integration test

A smaller-scope integration test that doesn't go through HTTP — just exercises the watcher directly against a tmp folder containing a mix of accepted / noise / unsupported files and verifies the row written to `watched_folders`. This complements the unit test from Task 3 by covering the realistic file layout the spec calls out.

This test may already be exercised by Task 3's test if it's broad enough. Read what you wrote in Task 3 first and decide whether this task is redundant. If Task 3's test already covers the realistic mix (PDF + Markdown + .canvas + .excalidraw.md + AppleDouble + dotfile), mark this task as "covered by Task 3" and skip it — but explicitly note that in the Task 3 test's docstring.

If the Task 3 test was minimal (e.g. only one unsupported extension), expand it OR add a second test here for the realistic mix:

```python
def test_scan_folder_realistic_obsidian_vault_layout(factory, tmp_path):
    """Realistic vault layout: prose markdown, a wikilink doc, a canvas, an
    excalidraw note, a .obsidian/ subdir, plus an AppleDouble shadow. Only
    the .canvas is counted; everything else is either ingested or noise."""
    # ... layout setup ...
    # Assert skipped_count == 1, skipped_extensions == [".canvas"]
```

- [ ] **Step 1: Decide redundancy with Task 3**

Read your Task 3 test. If it covers the realistic mix, skip this task and update the task tracker.

- [ ] **Step 2 (if not skipped): Write the test**

Append to `tests/watcher/test_main.py` (verbatim shape above; flesh out file layout per the docstring).

- [ ] **Step 3: Run + commit**

```bash
git add tests/watcher/test_main.py
git commit -m "test(watcher): realistic vault layout exercises skip tracking"
```

---

### Task 7: Phase 5 verification

- [ ] **Step 1: Lint + format**

Run:

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Both must pass.

- [ ] **Step 2: Backend test suite**

Run:

```bash
uv run pytest tests/ -q -m "not integration and not requires_models"
```

Expected: 879 (Phase 4 baseline) + Phase 5's new tests, all passing.

- [ ] **Step 3: Frontend type-check + lint**

```bash
cd frontend
npm run lint
npm run type-check
npm run format:check
```

All must pass.

- [ ] **Step 4: Migration sanity**

Run:

```bash
uv run alembic history
```

Expected output (last two lines):

```
0002_document_links -> 0003_watched_folder_skip_tracking (head), watched_folder skip tracking
0001_initial -> 0002_document_links, document_links
```

- [ ] **Step 5: Spot-check the UI**

Optional but reassuring: if a dev backend is reachable, start the app, look at `/folders`, confirm folders with no skipped files show NO line (no empty placeholder) and folders with skipped files show the muted message exactly once.

---

### Task 8: Phase 5 final holistic review

Per the standing fresh-eyes-review directive, dispatch a `feature-dev:code-reviewer` subagent against the branch tip with a MINIMAL prompt (no focus areas, no carve-outs, no "ignore X" instructions). Reference the Phase 5 plan + spec, give the commit range, and let the reviewer flag anything ≥ 80 confidence. Address findings before opening the consolidated PR.

The minimal prompt should be exactly:

> Review the Phase 5 skipped-file-hygiene changes on the `feat/markdown-text-handling` branch in worktree `<worktree-path>`. Scope: every commit added in Phase 5 (run `git log --oneline <phase-4-tip>..HEAD` to get the range — the Phase 4 tip is `cac3681`, the Phase 5 review fixup commit). Project: Harbor Clerk, single-tenant document dropbox. The plan being implemented is at `docs/superpowers/plans/2026-05-22-markdown-text-handling-phase-5.md` and the design spec is at `docs/superpowers/specs/2026-05-22-markdown-text-handling-design.md`. Read the diff and the changed files.

---

## Self-Review

Checked against the spec's Phase 5:

- **Track skips via two new `watched_folders` columns:** Task 1 — `skipped_count` (Integer, default 0) + `skipped_extensions` (`ARRAY(Text)`, default `'{}'`). The migration is `0003` (not `0002` as the spec said) because Phase 4 used `0002_document_links`.
- **During `_scan_folder`, files rejected for an unsupported extension are counted and their extensions collected:** Task 3 — exactly that, with the `classify_skip` helper (Task 2) providing the boundary between NOISE (uncounted) and UNSUPPORTED_EXTENSION (counted).
- **Dotfiles and `__MACOSX` excluded from the count:** the `SkipReason.NOISE` arm covers dotfiles, AppleDouble, `__MACOSX`, AND Excalidraw — strictly more conservative than the spec's wording (the spec mentions dotfiles + `__MACOSX`; we also cover the other intentional filters consistently).
- **Per-folder summary written to `watched_folders`:** Task 3 — `skipped_count` + `skipped_extensions` written alongside `last_scan_at` on scan completion.
- **`_folder_to_dict` returns both fields:** Task 4.
- **`FoldersPage.tsx` shows a small muted line when `skipped_count > 0`:** Task 5 — exactly the spec's message shape ("N files not ingested — unsupported types: .canvas, .excalidraw"), with overflow truncation for long lists.
- **Allowlist tests cover the spec's testing strategy:** Phase 1 already added comprehensive allowlist + Excalidraw guard tests; Phase 5 adds the per-folder tallying tests.
- **Obsidian-vault fixture:** Task 6 covers the realistic layout (PDF + Markdown + `.canvas` + `.excalidraw.md` + dotfile + AppleDouble), either as the Task 3 test or as a separate test.

No placeholders. Type names consistent (`SkipReason`, `classify_skip`, `skipped_count`, `skipped_extensions`). Migration revision id (`0003_watched_folder_skip_tracking`) consistent across the file, the test assertion in Task 7 Step 4, and the `down_revision` chain.

**Out of scope (deferred to future work):**
- Reevaluating which newly-added extensions warrant format-specific handling (e.g. `.srt` timestamp stripping, `.ipynb` JSON structure, `.canvas` JSON text-node extraction). Tracked in the design spec's Deferred section and will be captured in `pr_followups.md` when the PR opens.
- Surfacing wikilink backlinks via MCP / UI (Phase 4 deferred follow-up, also for `pr_followups.md`).
