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

**Unique constraint:** `(folder_id, relative_path)` — one entry per path per folder.

### Modified Table: `ingestion_jobs`

| Column | Type | Notes |
|---|---|---|
| `priority` | SmallInteger DEFAULT 0 | Lower = higher priority. GUI uploads: 0, watched folder: 10 |

Worker claim query changes from `ORDER BY created_at ASC` to `ORDER BY priority ASC, created_at ASC`.

### Existing Fields Used

- `DocumentVersion.source_path` — stores the absolute path to the watched file (already exists)
- `Upload.source` — uses existing `watch_folder` enum value (already exists)
- `Document` — no changes; watched folder documents are regular documents

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
| File created | Check extension against `ALLOWED_EXTENSIONS`. Hash file. Call `POST /api/watch/ingest` |
| File modified | Hash file. Compare against known SHA256. If different, call `POST /api/watch/ingest` |
| File renamed (within folder) | Resolve bookmark to new path. Call `POST /api/watch/rename` |
| File deleted / moved out | Resolve bookmark. If fails or new path outside folder, call `POST /api/watch/remove` |
| Folder renamed | Folder bookmark resolves transparently. Update stored path via API |

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
| `/api/watch/folders` | POST | Add a watched folder (path, bookmark_data) |
| `/api/watch/folders/{id}` | PATCH | Update folder (enable/disable, update last_event_id) |
| `/api/watch/folders/{id}` | DELETE | Remove folder and soft-delete all its tracked files |
| `/api/watch/folders/{id}/rescan` | POST | Trigger full rescan (resets last_event_id to null) |
| `/api/watch/ingest` | POST | File detected or changed — create/update document |
| `/api/watch/remove` | POST | File removed — soft-delete tracked file |
| `/api/watch/rename` | POST | File renamed — update relative_path and bookmark |

### Ingest Flow (`POST /api/watch/ingest`)

Input: `folder_id`, `relative_path`, `sha256`, `bookmark_data`, `source_path`

1. Look up existing `watched_files` entry by `(folder_id, relative_path)`
2. If exists and SHA256 matches → skip (no change)
3. If exists and SHA256 differs (content changed):
   - Delete all derived data for old version (chunks, embeddings, entities, summary)
   - Create new DocumentVersion with `source_path`, no `original_object_key`
   - Update `documents.latest_version_id`
   - Update `watched_files.sha256`
   - Enqueue `extract` stage with priority 10
4. If new file:
   - Create Document (title from filename; if nested, prefix with relative directory)
   - Create DocumentVersion with `source_path`
   - Create Upload record (source=`watch_folder`)
   - Create `watched_files` row
   - Enqueue `extract` stage with priority 10
5. If previously removed (`status = 'removed'`) and file reappears:
   - Flip status back to `active`, clear `removed_at`
   - Re-activate document (set status back to `active`)
   - Re-ingest if SHA256 differs

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

At the point where pipeline stages read the source file (extraction, OCR), add a single conditional:

```python
if version.source_path and os.path.exists(version.source_path):
    # Read directly from filesystem
    file_bytes = Path(version.source_path).read_bytes()
elif version.original_object_key:
    # Read from storage backend (existing path)
    resp = storage.get_object(bucket, version.original_object_key)
    file_bytes = resp.data
else:
    # Source unavailable — mark error
    raise SourceFileUnavailable(version.version_id)
```

Tika extraction already accepts file content as bytes. OCR stages work with in-memory data. This is a narrow change at the file-read boundary, not a storage backend abstraction change.

### Priority-Aware Job Claiming

Worker's `_claim_job` query changes:

```sql
-- Before:
ORDER BY created_at ASC

-- After:
ORDER BY priority ASC, created_at ASC
```

GUI uploads (priority 0) naturally claim ahead of watched folder jobs (priority 10). In-progress jobs complete without preemption.

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
| File deleted then recreated (same name, different content) | Old bookmark fails → soft-delete old entry. New file detected as creation → new document. |
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

---

## Migration

Single Alembic migration adding:
- `watched_folders` table
- `watched_files` table
- `priority` column on `ingestion_jobs` (SmallInteger, DEFAULT 0, NOT NULL)
- Index on `ingestion_jobs(priority, created_at)` for efficient claim ordering

---

## Testing

**Python (pytest):**
- Ingest endpoint: new file, changed file, unchanged file (skip), removed file, resurrection
- Priority ordering: verify watched folder jobs sort after GUI upload jobs
- Reaper: verify 30-day cleanup of removed watched files
- Pipeline source_path fallback: verify file read from disk when source_path is set

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
