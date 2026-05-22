# Phase 1: Extension Coverage — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Broaden and de-duplicate Harbor Clerk's file-extension allowlist so 17 additional text formats ingest, and guard against Obsidian Excalidraw files.

**Architecture:** Introduce one dependency-free module, `harbor_clerk.file_types`, as the single source of truth for the extension allowlist and routing sets. The API layer and the watcher both import it (replacing two hand-synced copies). The extract stage routes every plain-text format through its no-Tika path via a shared predicate. `*.excalidraw.md` files are rejected at both ingest entry points.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy, pytest, ruff. No new third-party dependencies in this phase.

**Working directory:** the `feat/markdown-text-handling` worktree. All paths below are relative to its root. Commands assume PostgreSQL is reachable (the test suite's `conftest.py` auto-creates the test DB on port 5433 or 5432).

**Spec:** `docs/superpowers/specs/2026-05-22-markdown-text-handling-design.md` (Phase 1).

---

## File Structure

- **Create** `src/harbor_clerk/file_types.py` — shared allowlist + routing sets + `is_excalidraw()`. Imports nothing from `harbor_clerk` (so the watcher can use it without an API dependency chain).
- **Create** `tests/test_file_types.py` — unit tests for the module.
- **Modify** `src/harbor_clerk/api/routes/uploads.py` — delete the local `ALLOWED_EXTENSIONS` set; import it from `file_types`.
- **Modify** `src/harbor_clerk/watcher/events.py` — delete the local `_ALLOWED_EXTENSIONS` set; import from `file_types`; add the Excalidraw guard to `_should_ignore`.
- **Modify** `tests/watcher/test_events.py` — extend `TestShouldIgnore`.
- **Modify** `src/harbor_clerk/worker/stages/extract.py` — add the `is_plain_text_source()` predicate; route the extraction dispatch, heading-skip, and OCR-skip through it.
- **Modify** `tests/test_extract_helpers.py` — test `is_plain_text_source()`.
- **Modify** `src/harbor_clerk/api/routes/watch.py` — reject `*.excalidraw.md` in the `/watch/ingest` endpoint.
- **Modify** `tests/test_api_watch.py` — test the ingest rejection.

Build order: Task 1 → 2 → 3 → 4 → 5 → 6. Tasks 2–5 each depend only on Task 1.

---

### Task 1: Create the shared `file_types` module

**Files:**
- Create: `src/harbor_clerk/file_types.py`
- Test: `tests/test_file_types.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_file_types.py`:

```python
"""Unit tests for the shared file-type classification module."""

from harbor_clerk.file_types import (
    ALLOWED_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    PLAIN_TEXT_EXTENSIONS,
    is_excalidraw,
)


def test_legacy_extensions_still_allowed():
    """Every extension from the pre-existing allowlist must remain accepted."""
    legacy = {
        ".pdf", ".docx", ".doc", ".rtf", ".txt", ".md", ".odt", ".pages",
        ".xlsx", ".xls", ".ods", ".numbers", ".csv",
        ".pptx", ".ppt", ".odp", ".key",
        ".jpg", ".jpeg", ".png", ".tiff", ".tif",
        ".epub", ".html", ".htm", ".eml",
    }
    assert legacy <= ALLOWED_EXTENSIONS


def test_new_extensions_added():
    """The broadened text formats are now accepted."""
    new = {
        ".markdown", ".tsv", ".srt", ".vtt", ".rst", ".org", ".adoc",
        ".tex", ".py", ".json", ".yaml", ".yml", ".toml", ".xml",
        ".log", ".ipynb", ".canvas",
    }
    assert new <= ALLOWED_EXTENSIONS


def test_plain_text_is_subset_of_allowed():
    assert PLAIN_TEXT_EXTENSIONS <= ALLOWED_EXTENSIONS


def test_markdown_set_is_exact():
    assert MARKDOWN_EXTENSIONS == {".md", ".markdown"}
    assert MARKDOWN_EXTENSIONS <= PLAIN_TEXT_EXTENSIONS


def test_new_text_formats_route_to_plain_text():
    for ext in (".rst", ".py", ".json", ".srt", ".canvas", ".markdown"):
        assert ext in PLAIN_TEXT_EXTENSIONS


def test_tika_formats_not_plain_text():
    """Office / PDF / image formats must NOT be on the plain-text path."""
    for ext in (".pdf", ".docx", ".xlsx", ".html", ".epub", ".png"):
        assert ext not in PLAIN_TEXT_EXTENSIONS


def test_is_excalidraw_true():
    assert is_excalidraw("Diagram.excalidraw.md") is True
    assert is_excalidraw("vault/sub/Sketch.EXCALIDRAW.MD") is True


def test_is_excalidraw_false():
    assert is_excalidraw("notes.md") is False
    assert is_excalidraw("report.pdf") is False
    assert is_excalidraw("my.excalidraw.txt") is False
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_file_types.py -v`
Expected: collection error / `ModuleNotFoundError: No module named 'harbor_clerk.file_types'`.

- [ ] **Step 3: Create the module**

Create `src/harbor_clerk/file_types.py`:

```python
"""Shared file-type classification: the extension allowlist and routing sets.

This module imports nothing from ``harbor_clerk`` so that both the API layer
(``api/routes/uploads.py``, ``api/routes/watch.py``) and the watcher
(``watcher/events.py``) can import it without creating a dependency chain.
It is the single source of truth — do not re-declare these sets elsewhere.
"""

# Extensions extracted as plain UTF-8 text (no Apache Tika).
PLAIN_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".srt",
        ".vtt",
        ".rst",
        ".org",
        ".adoc",
        ".tex",
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".xml",
        ".log",
        ".ipynb",
        ".canvas",
    }
)

# Markdown-family extensions — these get the full Markdown treatment in Phase 2.
MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown"})

# Formats extracted via Apache Tika.
_TIKA_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".rtf",
        ".odt",
        ".pages",
        ".xlsx",
        ".xls",
        ".ods",
        ".numbers",
        ".pptx",
        ".ppt",
        ".odp",
        ".key",
        ".epub",
        ".html",
        ".htm",
        ".eml",
    }
)

# Image formats (OCR-only, no text extraction).
_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".tiff", ".tif"})

# The full set of accepted file extensions.
ALLOWED_EXTENSIONS: frozenset[str] = PLAIN_TEXT_EXTENSIONS | _TIKA_EXTENSIONS | _IMAGE_EXTENSIONS


def is_excalidraw(path: str) -> bool:
    """True for Obsidian Excalidraw notes (``*.excalidraw.md``).

    These files carry a large compressed-JSON blob rather than prose and would
    pollute the search index if ingested as Markdown, so they are skipped at
    every ingest entry point.
    """
    return path.lower().endswith(".excalidraw.md")
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/test_file_types.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/file_types.py tests/test_file_types.py
git commit -m "feat(file-types): shared module with broadened extension allowlist"
```

---

### Task 2: Point `uploads.py` at the shared module

`uploads.py` currently declares its own `ALLOWED_EXTENSIONS` (lines 41–77). Replace it with an import. This is a pure refactor — verified by an identity test plus the existing suite.

**Files:**
- Modify: `src/harbor_clerk/api/routes/uploads.py`
- Test: `tests/test_file_types.py`

- [ ] **Step 1: Add the identity regression test**

Append to `tests/test_file_types.py`:

```python
def test_uploads_route_uses_shared_allowlist():
    """uploads.py must reference the shared set, not re-declare its own copy."""
    from harbor_clerk.api.routes import uploads

    assert uploads.ALLOWED_EXTENSIONS is ALLOWED_EXTENSIONS
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_file_types.py::test_uploads_route_uses_shared_allowlist -v`
Expected: FAIL — `assert <set ...> is <frozenset ...>` (uploads still has its own copy).

- [ ] **Step 3: Edit `uploads.py`**

In `src/harbor_clerk/api/routes/uploads.py`, delete the entire block at lines 41–77 (the `# Note: a local copy ...` comment through the closing `}` of `ALLOWED_EXTENSIONS`).

Then add this import alongside the other `harbor_clerk` imports (after the `from harbor_clerk.config import ...` line):

```python
from harbor_clerk.file_types import ALLOWED_EXTENSIONS
```

The existing use at the old line ~97 (`if ext not in ALLOWED_EXTENSIONS:`) is unchanged — the name still resolves.

- [ ] **Step 4: Run tests, verify they pass**

Run: `uv run pytest tests/test_file_types.py tests/test_api_watch.py -v`
Expected: PASS. (`test_api_watch.py` exercises `watch.py`, which imports `ALLOWED_EXTENSIONS` from `uploads.py` — this confirms the re-export still resolves.)

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/uploads.py tests/test_file_types.py
git commit -m "refactor(uploads): import allowlist from shared file_types module"
```

---

### Task 3: Migrate the watcher and add the Excalidraw guard

`watcher/events.py` keeps its own `_ALLOWED_EXTENSIONS` (lines 30–66) and `_should_ignore` (lines 90–120). Replace the copy with the shared import and add the `*.excalidraw.md` guard.

**Files:**
- Modify: `src/harbor_clerk/watcher/events.py`
- Test: `tests/watcher/test_events.py`

- [ ] **Step 1: Write the failing tests**

In `tests/watcher/test_events.py`, add two methods inside the existing `class TestShouldIgnore:` (after `test_allowed_files_pass`):

```python
    def test_newly_allowed_extensions_pass(self):
        """Phase 1 broadened the allowlist — these text formats now ingest."""
        for name in ("notes.rst", "script.py", "config.toml", "data.json", "subs.srt", "doc.markdown"):
            assert _should_ignore(name) is False, name

    def test_excalidraw_md_ignored(self):
        """*.excalidraw.md carries a JSON blob, not prose — must be skipped."""
        assert _should_ignore("Diagram.excalidraw.md") is True
        assert _should_ignore("vault/sub/Sketch.excalidraw.md") is True
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `uv run pytest tests/watcher/test_events.py::TestShouldIgnore -v`
Expected: `test_newly_allowed_extensions_pass` FAILS (e.g. `.rst` is not yet allowed) and `test_excalidraw_md_ignored` FAILS (`.excalidraw.md` resolves to suffix `.md`, currently allowed → not ignored).

- [ ] **Step 3: Edit `events.py`**

In `src/harbor_clerk/watcher/events.py`:

(a) Delete lines 30–66 — the `# Kept in sync ...` comment through the closing `}` of `_ALLOWED_EXTENSIONS`.

(b) Add this import after `from harbor_clerk.models.watched import ...` (the last `harbor_clerk` import):

```python
from harbor_clerk.file_types import ALLOWED_EXTENSIONS, is_excalidraw
```

(c) In `_should_ignore`, replace the final two lines:

```python
    # Extension allowlist (lowercase comparison).
    suffix = Path(relative_path).suffix.lower()
    return suffix not in _ALLOWED_EXTENSIONS
```

with:

```python
    # Excalidraw notes (*.excalidraw.md) carry a JSON blob, not prose — skip.
    if is_excalidraw(relative_path):
        return True
    # Extension allowlist (lowercase comparison).
    suffix = Path(relative_path).suffix.lower()
    return suffix not in ALLOWED_EXTENSIONS
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/watcher/test_events.py -v`
Expected: PASS (all `TestShouldIgnore` tests, including the two new ones, plus the existing event-handling tests).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/events.py tests/watcher/test_events.py
git commit -m "refactor(watcher): use shared allowlist; skip *.excalidraw.md"
```

---

### Task 4: Route every plain-text format through the no-Tika path

`run_extract` decides extraction routing with `obj_key.endswith((".txt", ".md", ".csv"))`. Replace that with a shared predicate so all 20 plain-text extensions take the no-Tika path — and so the same predicate also suppresses Tika heading extraction and OCR for them.

**Files:**
- Modify: `src/harbor_clerk/worker/stages/extract.py`
- Test: `tests/test_extract_helpers.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_extract_helpers.py`, add `is_plain_text_source` to the import block at the top:

```python
from harbor_clerk.worker.stages.extract import (
    _alpha_ratio,
    _extract_headings_via_tika,
    _extract_via_tika,
    _paginate_text,
    _sanitize_external_string,
    is_plain_text_source,
)
```

Then append these tests to the end of the file:

```python
# --- is_plain_text_source ---


def test_is_plain_text_source_by_mime():
    assert is_plain_text_source("text/plain", "") is True


def test_is_plain_text_source_new_extensions():
    for key in ("notes.rst", "data.json", "script.py", "subs.srt", "graph.canvas", "doc.markdown"):
        assert is_plain_text_source("", key) is True, key


def test_is_plain_text_source_legacy_plain_text():
    for key in ("readme.txt", "notes.md", "table.csv"):
        assert is_plain_text_source("", key) is True, key


def test_is_plain_text_source_tika_formats_excluded():
    for key in ("report.pdf", "memo.docx", "sheet.xlsx", "page.html", "book.epub"):
        assert is_plain_text_source("", key) is False, key
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_extract_helpers.py -k is_plain_text_source -v`
Expected: collection error — `ImportError: cannot import name 'is_plain_text_source'`.

- [ ] **Step 3: Add the predicate to `extract.py`**

In `src/harbor_clerk/worker/stages/extract.py`:

(a) Add to the imports (after `from harbor_clerk.config import get_settings`):

```python
from harbor_clerk.file_types import PLAIN_TEXT_EXTENSIONS
```

(b) After the `IMAGE_MIMES = {...}` line (around line 23), add:

```python

# Filename suffixes extracted as plain UTF-8 text. A tuple because str.endswith
# requires one; sorted for deterministic ordering.
_PLAIN_TEXT_SUFFIXES = tuple(sorted(PLAIN_TEXT_EXTENSIONS))


def is_plain_text_source(mime: str, obj_key: str) -> bool:
    """True if the file should be extracted as plain UTF-8 text (no Tika).

    Single source of truth for three decisions in ``run_extract``: the
    extraction dispatch, skipping Tika heading extraction, and skipping OCR.
    """
    return mime == "text/plain" or obj_key.endswith(_PLAIN_TEXT_SUFFIXES)
```

- [ ] **Step 4: Wire the predicate into `run_extract`**

Three edits inside `run_extract`:

(a) The extraction dispatch — replace:

```python
        elif mime == "text/plain" or obj_key.endswith((".txt", ".md", ".csv")):
            # Plain text / Markdown / CSV — no Tika needed
            pages = _extract_txt(data)
```

with:

```python
        elif is_plain_text_source(mime, obj_key):
            # Plain-text formats — no Tika needed
            pages = _extract_txt(data)
```

(b) The heading-skip decision — replace:

```python
        skip_headings = is_image or mime in _SKIP_HEADINGS_MIMES or obj_key.endswith(_SKIP_HEADINGS_EXTS)
```

with:

```python
        skip_headings = (
            is_image
            or mime in _SKIP_HEADINGS_MIMES
            or obj_key.endswith(_SKIP_HEADINGS_EXTS)
            or is_plain_text_source(mime, obj_key)
        )
```

(c) The OCR-skip decision — replace:

```python
        is_never_ocr = is_rtf or mime in _NEVER_OCR_MIMES or obj_key.endswith(_NEVER_OCR_EXTS)
```

with:

```python
        is_never_ocr = (
            is_rtf
            or mime in _NEVER_OCR_MIMES
            or obj_key.endswith(_NEVER_OCR_EXTS)
            or is_plain_text_source(mime, obj_key)
        )
```

- [ ] **Step 5: Run the tests, verify they pass**

Run: `uv run pytest tests/test_extract_helpers.py -v`
Expected: PASS (existing helper tests plus the four new `is_plain_text_source` tests).

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/worker/stages/extract.py tests/test_extract_helpers.py
git commit -m "feat(extract): route all plain-text formats through the no-Tika path"
```

---

### Task 5: Reject `*.excalidraw.md` at the `/watch/ingest` endpoint

The macOS `/watch/ingest` endpoint validates the extension against `ALLOWED_EXTENSIONS`, but `*.excalidraw.md` resolves to the allowed `.md` suffix. Add an explicit guard reusing `is_excalidraw`.

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py`
- Test: `tests/test_api_watch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_watch.py`:

```python
async def test_ingest_rejects_excalidraw_md(client, admin_token, db_session, tmp_path):
    """*.excalidraw.md carries a JSON blob, not prose — /ingest must reject it."""
    folder = WatchedFolder(path=str(tmp_path), display_name="vault", bookmark_data=None)
    db_session.add(folder)
    await db_session.flush()

    resp = await client.post(
        "/api/watch/ingest",
        headers=auth_header(admin_token),
        json={
            "folder_id": str(folder.folder_id),
            "relative_path": "Diagram.excalidraw.md",
            "sha256": "00" * 32,
            "bookmark_data": "",
            "source_path": str(tmp_path / "Diagram.excalidraw.md"),
            "mime_type": "text/markdown",
        },
    )
    assert resp.status_code == 400
    assert "excalidraw" in resp.json()["detail"].lower()
```

- [ ] **Step 2: Run the test, verify it fails**

Run: `uv run pytest tests/test_api_watch.py::test_ingest_rejects_excalidraw_md -v`
Expected: FAIL — the request is not rejected at the extension check (`.md` is allowed), so the status is not 400.

- [ ] **Step 3: Edit `watch.py`**

In `src/harbor_clerk/api/routes/watch.py`:

(a) Replace the import line `from harbor_clerk.api.routes.uploads import ALLOWED_EXTENSIONS` with:

```python
from harbor_clerk.file_types import ALLOWED_EXTENSIONS, is_excalidraw
```

(b) In the `ingest_file` handler, replace:

```python
    # Validate extension
    ext = PurePosixPath(body.relative_path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")
```

with:

```python
    # Validate extension
    if is_excalidraw(body.relative_path):
        raise HTTPException(status_code=400, detail="Excalidraw drawings are not ingested")
    ext = PurePosixPath(body.relative_path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file extension: {ext}")
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/test_api_watch.py -v`
Expected: PASS (the new test plus all existing `/api/watch` tests).

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py tests/test_api_watch.py
git commit -m "feat(watch): reject *.excalidraw.md at the ingest endpoint"
```

---

### Task 6: Phase 1 verification

Confirm the whole phase is clean before handing off.

- [ ] **Step 1: Lint and format check**

Run: `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`
Expected: no errors. If `ruff format --check` reports a file, run `uv run ruff format src/ tests/`, then `git add -p` the formatting fix and amend it into the relevant task's commit (or make a `style:` commit).

- [ ] **Step 2: Run the full Phase 1 test set**

Run: `uv run pytest tests/test_file_types.py tests/test_extract_helpers.py tests/watcher/test_events.py tests/test_api_watch.py -v`
Expected: all PASS.

- [ ] **Step 3: Confirm no stray allowlist copies remain**

Run: `git grep -n "ALLOWED_EXTENSIONS = {" src/`
Expected: no output — the only definition now lives in `file_types.py` (as a `|` union, not a `{` literal).

---

## Self-Review

Checked against the spec's Phase 1:

- **1a — de-duplicate the allowlist:** Task 1 creates `file_types.py`; Tasks 2 and 3 delete the two copies and import it. Task 6 Step 3 guards against regression.
- **1b — add the 17 extensions:** Task 1 puts them in `PLAIN_TEXT_EXTENSIONS` ⊂ `ALLOWED_EXTENSIONS`; `tests/test_file_types.py::test_new_extensions_added` verifies.
- **1c — extraction routing:** Task 4 routes them through `_extract_txt` via `is_plain_text_source`, which also suppresses Tika headings and OCR for them.
- **1d — Excalidraw guard:** Task 3 covers the watcher path (`_should_ignore`); Task 5 covers the API ingest path.

No placeholders; every step contains the exact code or command. Type names are consistent across tasks (`ALLOWED_EXTENSIONS`, `PLAIN_TEXT_EXTENSIONS`, `MARKDOWN_EXTENSIONS`, `is_excalidraw`, `is_plain_text_source`). `MARKDOWN_EXTENSIONS` is defined here but consumed in Phase 2 — intentional.
