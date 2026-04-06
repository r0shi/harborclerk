# Watched Folders — Design Spec

**Date:** 2026-04-05
**Status:** Draft
**Scope:** macOS native app only (not Docker/Linux)

## Overview

Watched folders let users point Harbor Clerk at a directory on their Mac. Files in that directory are automatically ingested, tracked for changes, and removed when deleted — without copying originals into Harbor Clerk's storage. The watched folder boundary is the scope: files that move outside are treated as deletions.

This is a macOS-only feature. It relies on FSEvents for filesystem monitoring and macOS bookmark data for robust file identity tracking across renames and moves.

## Goals

- Zero-friction ingestion: drop files into a folder, they appear in Harbor Clerk
- Track renames, modifications, and deletions automatically
- No duplication of original files — reference in place
- Single version per watched file (no version history)
- GUI uploads take priority over watched folder ingestion
- Soft-delete with 30-day grace period when source files disappear

## Non-Goals

- Cross-platform support (Linux/Docker can be added later via Python watchdog)
- Per-folder file type filters (uses the global `ALLOWED_EXTENSIONS` set)
- Version history for watched files
- Watching network volumes or external drives (same-volume only for bookmark reliability)

---

## Data Model

### New Table: `watched_folders`

| Column | Type | Notes |
|---|---|---|
| `folder_id` | UUID PK | |
| `path` | Text NOT NULL | Resolved folder path (display/logging) |
| `bookmark_data` | LargeBinary NOT NULL | macOS bookmark for folder identity |
| `recursive` | Boolean DEFAULT true | Watch subdirectories |
| `enabled` | Boolean DEFAULT true | Pause/resume without removing |
| `last_event_id` | BigInteger NULL | FSEvents sinceWhen cursor for incremental replay |
| `last_scan_at` | Timestamp NULL | Last full or incremental scan completion |
| `created_at` | Timestamp NOT NULL | |

### New Table: `watched_files`

| Column | Type | Notes |
|---|---|---|
| `file_id` | UUID PK | |
| `folder_id` | UUID FK → watched_folders (CASCADE) | Parent folder |
| `relative_path` | Text NOT NULL | Path relative to watched folder root |
| `bookmark_data` | LargeBinary NOT NULL | macOS bookmark for file identity |
| `sha256` | Binary NOT NULL | Last known content hash |
| `doc_id` | UUID FK → documents NULL | Linked document |
| `version_id` | UUID FK → document_versions NULL | Current version |
| `status` | Enum('active', 'removed') DEFAULT 'active' | Soft delete state |
| `removed_at` | Timestamp NULL | When file was detected as removed |
| `created_at` | Timestamp NOT NULL | |
| `updated_at` | Timestamp NOT NULL | |

**Unique constraint:** `(folder_id, relative_path)` with a partial index on `status = 'active'` only. This allows a soft-deleted row and a new active row to coexist for the same path. Alternatively, the ingest endpoint hard-deletes the old soft-deleted `watched_files` row before inserting a new one (simpler, no data value in keeping soft-deleted rows after replacement).

**Chosen approach:** On file creation where a soft-deleted row exists for the same `(folder_id, relative_path)`, hard-delete the old row and insert a fresh one. This avoids partial index complexity and is correct because the old bookmark is stale anyway (the old file is gone, the new file has a new bookmark).

### Modified Table: `ingestion_jobs`

| Column | Type | Notes |
|---|---|---|
| `priority` | SmallInteger DEFAULT 0 | Lower = higher priority. GUI uploads: 0, watched folder: 10 |

Worker claim query changes from `ORDER BY created_at ASC` to `ORDER BY priority ASC, created_at ASC`.

### Schema Changes to Existing Tables

#### `document_versions`

The following columns are currently `NOT NULL` but must become nullable for watched-folder versions (which have no stored object):

- `original_sha256`: **Remove the UNIQUE constraint.** Watched folder files are identified by `watched_files.sha256` and bookmark data, not by version-level SHA256 uniqueness. Duplicate content across documents is legitimate (same PDF in two folders, or a watched file with same content as a GUI upload). The existing upload dedup logic (`_confirm_single`) continues to check SHA256 against existing versions *for GUI uploads only* — watched folder ingest skips that check since each watched file is tracked independently.
- `original_bucket`: Make nullable. Watched-folder versions set this to NULL.
- `original_object_key`: Make nullable. Watched-folder versions set this to NULL.

These columns remain NOT NULL for GUI-uploaded versions (enforced at the application layer, not the DB).

#### `_version_filename()` in `pipeline.py`

This helper calls `posixpath.basename(version.original_object_key)` and is used in SSE events and logging. Update to:

```python
def _version_filename(version: DocumentVersion) -> str:
    if version.original_object_key:
        return posixpath.basename(version.original_object_key)
    if version.source_path:
        return posixpath.basename(version.source_path)
    return "unknown"
```

#### `uploads`

`minio_bucket` and `minio_object_key` are `NOT NULL`. For watched-folder uploads, set these to empty string `""` (consistent with the existing pattern used for duplicate uploads in `_confirm_single`). No schema migration needed.

### Existing Fields Used

- `DocumentVersion.source_path` (Text, nullable) — stores the absolute path to the watched file (already exists)
- `Upload.source` — uses existing `watch_folder` enum value (already exists)
- `Upload.source_path` (Text, nullable) — stores the original path (already exists)
- `Document` — no schema changes; watched folder documents are regular documents

---

## Swift: FSEvents Watcher

### WatchedFolderManager

New singleton class, owned by ServiceManager. Starts after the API is healthy.

**Lifecycle:**
1. On API ready: fetch watched folders from `GET /api/watch/folders`
2. For each enabled folder: resolve bookmark to current path, start `FSEventStream` from stored `last_event_id` (or `kFSEventStreamEventIdSinceNow` if none)
3. For newly added folders (no `last_event_id`): full directory walk first, then start streaming

**FSEventStream configuration:**
- `kFSEventStreamCreateFlagFileEvents` — file-level events (not just directory)
- `kFSEventStreamCreateFlagUseCachedEvents` — replay from stored event ID
- Latency: 1.0 second (coalesce rapid changes)

**Event handling:**

| Event | Action |
|---|---|
| File created | Check file type (see File Type Detection below). Hash file. Call `POST /api/watch/ingest` with resolved MIME type. |
| File modified | Hash file. Compare against known SHA256. If different, call `POST /api/watch/ingest` |
| File renamed (within folder) | Resolve bookmark to new path. Call `POST /api/watch/rename` |
| File deleted / moved out | Resolve bookmark. If fails or new path outside folder, call `POST /api/watch/remove` |
| Folder renamed | Folder bookmark resolves transparently. Update stored path via API |

### File Type Detection

The Swift watcher uses macOS **Uniform Type Identifiers (UTType)** for file type detection, which is more robust than extension matching alone. UTType uses file extension, Finder metadata, extended attributes, and in some cases content sniffing (magic bytes).

**Detection flow:**
1. Read `URL.resourceValues(forKeys: [.contentTypeKey])` to get the file's `UTType`
2. Check if the UTType conforms to any of the allowed supertypes:
   - `UTType.pdf`, `.image`, `.plainText`, `.spreadsheet`, `.presentation`
   - `.html`, `.emailMessage`, `.epub`
   - Specific types: `.rtf`, `.commaSeparatedText`
   - Office types via MIME-to-UTType mapping for `.docx`, `.doc`, `.odt`, `.xlsx`, `.pptx`, etc.
3. If UTType detection returns no type or a generic type (`public.data`), **fall back to file extension** against the Python backend's `ALLOWED_EXTENSIONS` set
4. Send the resolved MIME type (from `UTType.preferredMIMEType` or extension-based guess) to the API alongside the file path, so the pipeline doesn't need to re-detect

**Benefit:** A PDF renamed to `.txt` is still correctly identified as a PDF. A `.pages` or `.numbers` file that macOS understands via UTType gets handled even if extension-to-MIME mapping is imperfect.

The Swift side fetches the `ALLOWED_EXTENSIONS` set once from `GET /api/watch/allowed-extensions` on startup for the extension fallback path.

**Event ID persistence:**
- After processing a batch of events, update `last_event_id` via `PATCH /api/watch/folders/{id}`
- All state lives in the DB, not config.json

**Rate limiting for initial scans:**
- Batch files in groups of 20 to the API
- 100ms delay between batches for UI responsiveness
- Job queue handles pipeline backpressure naturally

### Preferences UI

New "Watched Folders" section in PreferencesWindow (below existing settings):

- **Add Folder** button → `NSOpenPanel` (directory mode) → creates bookmark, calls `POST /api/watch/folders`, starts watcher
- **Folder list** showing: path, file count, last scan time, enabled state
- **Per-folder controls:** enable/disable toggle, "Rescan Now" button, remove button
- **Rescan Now** triggers `POST /api/watch/folders/{id}/rescan` → full directory walk ignoring cached event ID

---

## Python API

### New Route File: `src/harbor_clerk/api/routes/watch.py`

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/watch/folders` | GET | List watched folders with file counts and status |
| `/api/watch/folders` | POST | Add a watched folder (path, bookmark_data). Rejects overlapping folders. |
| `/api/watch/folders/{id}` | PATCH | Update folder (enable/disable, update last_event_id) |
| `/api/watch/folders/{id}` | DELETE | Remove folder and soft-delete all its tracked files |
| `/api/watch/folders/{id}/rescan` | POST | Trigger full rescan (resets last_event_id to null) |
| `/api/watch/ingest` | POST | File detected or changed — create/update document |
| `/api/watch/remove` | POST | File removed — soft-delete tracked file |
| `/api/watch/rename` | POST | File renamed — update relative_path and bookmark |
| `/api/watch/allowed-extensions` | GET | Return the `ALLOWED_EXTENSIONS` set for Swift-side filtering |

### Authentication & Security

All watch endpoints require authentication via `require_user` (same as upload endpoints). The Swift `WatchedFolderManager` authenticates using the local admin user's credentials (stored in the native app's keychain, same mechanism used for other local API calls).

**Path traversal protection:** The `POST /api/watch/ingest` endpoint validates that `source_path` is a descendant of the watched folder's `path` before any file operations. Specifically:
- Resolve `source_path` to an absolute, canonical path (resolving symlinks)
- Verify it starts with the watched folder's resolved path
- Reject with 400 if validation fails

The download endpoint (`GET /api/docs/{id}/download`) applies the same validation when serving from `source_path`.

### Ingest Flow (`POST /api/watch/ingest`)

Input: `folder_id`, `relative_path`, `sha256`, `bookmark_data`, `source_path`, `mime_type`

1. Validate `source_path` is within the watched folder's path (see Security above)
2. Look up existing `watched_files` entry by `(folder_id, relative_path)`
3. If exists with `status = 'active'` and SHA256 matches → skip (no change)
4. If exists with `status = 'active'` and SHA256 differs (content changed):
   - Create new DocumentVersion with `source_path` set, `original_bucket`/`original_object_key` NULL
   - Update `documents.latest_version_id` to the new version
   - **Keep old version's derived data until new version reaches `finalize`** — the old version remains searchable during re-processing. On `finalize` of new version: delete old version and its derived data. This prevents a gap where the document has no searchable content.
   - Update `watched_files.sha256` and `watched_files.version_id`
   - Enqueue `extract` stage with priority 10
5. If new file (no existing `watched_files` row, or only a `status = 'removed'` row):
   - If a soft-deleted row exists for this `(folder_id, relative_path)`: hard-delete it (old bookmark is stale)
   - Create Document (title from filename; if nested, prefix with relative directory path)
   - Create DocumentVersion with `source_path` set, `original_bucket`/`original_object_key` NULL, `original_sha256` from the computed hash, `mime_type` from the Swift-provided value
   - Create Upload record (source=`watch_folder`, `minio_bucket`/`minio_object_key` = `""`, `user_id` from authenticated principal, `mime_type` from Swift)
   - Create `watched_files` row
   - Enqueue `extract` stage with priority 10
6. If previously removed (`status = 'removed'`) and file reappears with same bookmark:
   - Flip status back to `active`, clear `removed_at`
   - Re-activate document (set `documents.status` back to `active`)
   - Update `source_path` on the existing DocumentVersion (Trash restore may change the absolute path)
   - Update `watched_files.bookmark_data` with the fresh bookmark
   - If SHA256 differs: re-ingest (step 4 flow)
   - If SHA256 matches: no re-ingest needed, derived data is still intact

### Priority Propagation

`enqueue_stage()` gains an optional `priority: int = 0` parameter. When a watched-folder file triggers ingestion, the initial `extract` call passes `priority=10`. The `advance_pipeline()` function propagates the priority from the completed job to all downstream stages it enqueues. This ensures the entire pipeline for a watched-folder file runs at lower priority than GUI uploads.

### Remove Flow (`POST /api/watch/remove`)

1. Set `watched_files.status = 'removed'`, `removed_at = now()`
2. Set `documents.status = 'removed'`
3. Document's derived data (chunks, embeddings) remains intact for 30 days

### Reaper Addition

In the existing reaper loop (`src/harbor_clerk/api/app.py`):

- Query `watched_files WHERE status = 'removed' AND removed_at < now() - 30 days`
- For each: hard-delete the document, all versions, and all derived data
- Delete the `watched_files` row

---

## Pipeline Integration

### Source Path File Reading

At the point where pipeline stages read the source file (extraction, OCR), add a conditional before the existing storage read:

```python
if version.source_path and os.path.exists(version.source_path):
    # Read directly from filesystem (watched folder files)
    file_bytes = Path(version.source_path).read_bytes()
elif version.original_object_key:
    # Read from storage backend (GUI-uploaded files)
    resp = storage.get_object(bucket, version.original_object_key)
    file_bytes = resp.data
else:
    # Source unavailable — mark error
    raise SourceFileUnavailable(version.version_id)
```

This check must be inserted before the existing `storage.get_object()` call in `extract.py` (and `ocr.py` if it reads the original). Tika extraction already accepts file content as bytes. OCR stages work with in-memory data.

### Priority-Aware Job Claiming

Worker's `_claim_job` query changes:

```sql
-- Before:
ORDER BY created_at ASC

-- After:
ORDER BY priority ASC, created_at ASC
```

GUI uploads (priority 0) naturally claim ahead of watched folder jobs (priority 10). In-progress jobs complete without preemption.

### Re-Ingestion Version Swap

When a watched file's content changes, a new DocumentVersion is created while the old one remains active. The `finalize` stage for the new version performs cleanup:

1. Set `documents.latest_version_id` to the new version (already done at ingest time for search freshness)
2. Delete the old version's derived data (chunks, embeddings, entities, summary)
3. Delete the old DocumentVersion row

This ensures the document is always searchable — old data serves until new data is ready.

---

## Frontend Changes

### Upload Page — macOS Hint

Detect native app context:
```typescript
const isNativeApp = !!window.webkit?.messageHandlers;
```

When `isNativeApp` is true, render a subtle info banner below the upload dropzone:

> "Tip: Set up watched folders in Harbor Clerk Server preferences to automatically ingest files from your Mac."

Does not appear in browser or Docker deployments.

### Documents Page — Watched File Indicators

- Documents linked to a `watched_files` entry display a small link icon beside the title
- Tooltip on hover shows the source path: "Watched: ~/Documents/Work/Invoices/receipt.pdf"
- Download button serves the file from `source_path` via the API (API reads from disk, streams to client)
- Documents with `removed` status render muted with "Source file removed" text
- No separate tab or filter — watched documents are part of the normal corpus

### API Support for Frontend

- `GET /api/docs` response includes `watch_source_path` (nullable) and `watch_status` (nullable) fields on documents that have a `watched_files` link
- Download endpoint (`GET /api/docs/{id}/download`) checks `source_path` first, falls back to storage backend

---

## Edge Cases

| Scenario | Behavior |
|---|---|
| File moved within watched folder | Bookmark resolves to new path; update `relative_path`. Document title unchanged (user can rename manually). |
| File moved outside watched folder | Bookmark resolves to path outside folder boundary → treat as deletion (soft-delete) |
| File moved to different volume | Bookmark fails to resolve → treat as deletion (soft-delete) |
| File deleted then recreated (same name, different content) | Old bookmark fails → soft-delete old entry. New file detected as creation → hard-delete old `watched_files` row, create new document. |
| File replaced (same name, same content) | SHA256 matches → no action |
| Watched folder itself renamed | Folder bookmark resolves to new path. Update stored path. Watchers continue. |
| Watched folder deleted | Folder bookmark fails → disable watcher, surface error in preferences UI |
| App quit, files changed, app relaunched | FSEvents replays from stored `last_event_id`. Catches up on missed changes. |
| App quit, files changed, too many events (FSEvents overflow) | FSEvents reports `kFSEventStreamEventFlagMustScanSubDirs` → trigger full rescan for that folder |
| Overlapping watched folders | Prevent at add time: reject if new folder is a parent or child of existing watched folder |
| Unsupported file type added to folder | Silently ignored (filtered by `ALLOWED_EXTENSIONS`) |
| Very large file (>200MB) | Apply same `max_file_size_mb` limit as uploads. Skip with warning logged. |
| Permission denied on file | Log warning, skip file, continue scan |
| Source file unavailable at pipeline time | Mark ingestion job as error, set watched_file status to removed |
| Duplicate SHA256 (same content in watched folder and GUI upload) | Both documents exist independently. No dedup — they are separate documents from different sources. |
| File restored from Trash (same bookmark resolves) | Resurrection flow: re-activate, refresh `source_path` (may differ post-restore), refresh bookmark data |

---

## Migration

Single Alembic migration adding:
- `watched_folders` table
- `watched_files` table
- `priority` column on `ingestion_jobs` (SmallInteger, DEFAULT 0, NOT NULL)
- Index on `ingestion_jobs(priority, created_at)` for efficient claim ordering
- Drop UNIQUE constraint on `document_versions.original_sha256`
- Make `document_versions.original_bucket` nullable
- Make `document_versions.original_object_key` nullable

---

## Testing

**Python (pytest):**
- Ingest endpoint: new file, changed file, unchanged file (skip), removed file, resurrection
- Ingest with soft-deleted row for same path: old row hard-deleted, new row created
- Priority ordering: verify watched folder jobs sort after GUI upload jobs
- Priority propagation: verify downstream stages inherit priority from initial enqueue
- Reaper: verify 30-day cleanup of removed watched files
- Pipeline source_path fallback: verify file read from disk when source_path is set
- Path traversal protection: verify source_path outside watched folder is rejected
- Re-ingest version swap: verify old version's derived data persists until new version finalizes
- Auth: verify watch endpoints require authenticated user

**Swift (XCTest):**
- WatchedFolderManager: bookmark creation/resolution, event ID persistence
- Preferences UI: add/remove/enable/disable folders

**Manual:**
- Add a folder with mixed file types → only supported types ingested
- Rename a file in Finder → document tracks the rename
- Delete a file → document shows as removed
- Wait 30+ days (or adjust reaper interval) → document hard-deleted
- Quit app, add files, relaunch → files picked up from FSEvents replay
- Upload via GUI while watched folder scan is running → GUI upload processes first
- Same file in watched folder and uploaded via GUI → both exist as separate documents
