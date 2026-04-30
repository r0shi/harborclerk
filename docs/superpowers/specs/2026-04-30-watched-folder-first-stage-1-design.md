# Watched-Folder-First Refactor — Stage 1 Design

## Context

Direct upload via the web UI is currently the primary way users add documents to Harbor Clerk. Watched folders exist on macOS (FSEvents-based, Swift implementation in `WatchedFolderManager`) but are a secondary path. The product direction is to invert this: watched folders become the only user-facing ingest path, while the direct-upload code path is preserved internally for future non-user-facing sources (e.g., email ingestion).

The four pending tasks tracked for this work (folder filter on chat/search, upload-to-folder flow on macOS, Docker watched folders, deprecate direct upload) span more than one cohesive change. This document carves out the first stage and sketches what comes after.

## Umbrella: three sequenced stages

1. **Stage 1 — watcher unification + folder management graduates to web app** (this spec).
   Bring watched folders to Docker, replace the Swift FSEvents watcher with a Python `watchdog`-based daemon, move folder management UI from the macOS menubar Preferences into the main web app at a new top-level "Folders" tab.
2. **Stage 2 — watched-folder-only UX** (separate spec).
   Onboarding wizard ("pick a folder to watch"), remove the direct-upload UI from the web app entirely. `/api/uploads/*` endpoints stay alive for non-user paths (future email ingestion, automation, tests). Depends on Stage 1 because Docker users need watched folders to work before we can hide the upload UI from them.
3. **Stage 3 — flatten document model** (separate spec, design TBD).
   Remove `document_versions` and re-key all child tables (`ingestion_jobs`, `document_pages`, `chunks`, `document_headings`, `entities`, `watched_files`, `uploads`) to `doc_id`. Pulls pipeline state up to the document row. Independent of Stages 1 and 2; can ship in any order. Pros/cons of the data-model change versus a UI-only flattening to be discussed when we get there.

The folder filter task ("#134") is folded into the Projects/Collections work (#97) rather than shipped standalone — folder scoping for human users is the same dimension already implemented for API keys (`KeyScope.scope_folder_ids`, `apply_key_scope`), and the right time to expose it interactively is when projects/collections are the unit of organization.

Migration of pre-existing direct-uploaded documents is explicitly out of scope. There are no real users with significant legacy direct uploads; we don't write retroactive folder-assignment code.

## Stage 1 scope

In scope:
- New Python watcher daemon `harbor-clerk-watcher` using `watchdog`. Runs on both macOS and Docker, replaces Swift's `WatchedFolderManager` FSEvents code.
- New Docker compose service `watcher`, mounts a configurable `WATCH_ROOT` (default `/data/watch`). Top-level subdirs of `WATCH_ROOT` are auto-discovered as watched folders.
- Single Python watcher implementation; on macOS it's bundled by the Server menubar app as a managed subprocess via `ServiceManager.swift`.
- macOS folder picker: WKWebView ↔ Swift JS bridge calling `NSOpenPanel`. No bookmark data — the Server menubar app (and its Python watcher child) relies on Full Disk Access already granted at install time for Postgres + log writes. NSOpenPanel is used only to pick a path; the path is then watched directly by the Python child process.
- Docker folder management is "view only" in the UI: folders are auto-discovered from mount points; the UI shows a doc link instead of an "Add Folder" button. Per-folder enable/disable + delete (only for unavailable mounts) are still actions.
- New top-level **Folders** tab in the web app (replaces the existing "Upload" tab in the nav). The Upload page stays mounted at `/upload` but is unlinked — Stage 2 will remove it.
- Per-folder progress UI: total / completed counter, per-stage breakdown when expanded, live updates via SSE.
- Documents page gains a folder display column and a lightweight client-side folder filter chip. Full scope semantics defer to the Projects/Collections work.
- Operator docs explaining how Docker users add a folder by mounting it.
- Retire `WatchedFolderManager.swift` and the macOS Preferences "Watched Folders" section.

Out of scope (Stage 2 / Stage 3 / unrelated):
- Hiding or removing the direct-upload web UI.
- Onboarding wizard.
- Removing `document_versions`.
- Email or other non-FS ingest sources.
- Install-path / dylib rpath rationalization (separate cleanup, see `project_install_path_revisit` memory note).

## Architecture

### Watcher daemon

New module `src/harbor_clerk/watcher/`:

- `main.py` — entry point, signal handling, observer lifecycle.
- `observer.py` — wraps `watchdog.observers.Observer` (auto-picks FSEvents on macOS, inotify on Linux). On per-folder failure to install a native watch (NFS / fuse / etc.), falls back to `watchdog.observers.polling.PollingObserver` for that folder.
- `events.py` — translates filesystem events into `watched_files` upserts + ingestion-job enqueues. New file → enqueue `extract`. Modified file with new sha256 → enqueue re-process (mechanics here change in Stage 3 once versions are gone). Deleted file → mark `watched_files.status='removed'`.
- `discovery.py` — Docker auto-discovery loop. Walks `WATCH_ROOT/*` at startup and every 60s. Inserts new top-level subdirs into `watched_folders` with `auto_discovered=true`. Marks folders whose path no longer exists as `enabled=false` with `unavailable_reason='unmounted'`.
- `db_listener.py` — `LISTEN watched_folders_changed` to react to folder add/remove/enable/disable from the API without restarting the daemon.

New entry point in `pyproject.toml`:

```toml
[project.scripts]
harbor-clerk-watcher = "harbor_clerk.watcher.main:main"
```

### Process placement

- **macOS**: managed by `ServiceManager.swift` as a new `WatcherService`, started after Postgres + workers, stopped before them on shutdown. Replaces the now-deleted `WatchedFolderManager`.
- **Docker**: new compose service `watcher`, `command: harbor-clerk-watcher`, `depends_on: [postgres]`, env `WATCH_ROOT=/data/watch`, with the operator's bind mounts under that path.

### Folder picking

- **macOS**: `Harbor Clerk.app` (the WKWebView client) registers a `WKScriptMessageHandler` for a `pickFolder` channel. The web "Add Folder" button calls `window.harborclerk.pickFolder()` which posts a message to Swift; Swift runs `NSOpenPanel` (canChooseDirectories = true), returns the chosen path string back via `evaluateJavaScript` resolving a JS Promise. The web UI then calls `POST /api/watch/folders` with the chosen path. Note: NSOpenPanel here is only doing path selection — the security-scoped bookmark workflow that the existing Swift watcher uses is not needed because the Server app + its watcher child run with Full Disk Access (required already for Postgres data dir, log writes). If FDA is missing, the watcher logs the failure on the first denied path and surfaces it as `unavailable_reason='access_denied'` in the UI; user is prompted to grant FDA in System Settings.
- **Docker**: no picker. UI shows an info bar with a doc link explaining the `volumes:` model.

### Frontend platform-aware shape

A new `GET /api/watch/system` endpoint returns:

```json
{ "platform": "macos" | "docker",
  "picker": "native" | "none",
  "watch_root": "/data/watch" }
```

The Folders page reads this once and switches between "Add Folder" button vs. info bar.

## Data model

Single migration `0014_watcher_unification`:

`watched_folders`:
- `bookmark_data BYTEA NOT NULL` → `BYTEA NULL`. Existing rows keep their bookmark blob; nothing reads it after this migration. Column is dropped in a later cleanup.
- ADD `unavailable_reason TEXT NULL` — `"unmounted"` for Docker folders whose path disappeared, otherwise null. Surfaces as a status pill.
- ADD `display_name TEXT NULL` — defaults to basename of `path`. User-editable on macOS, fixed to subdir name on Docker.
- ADD `auto_discovered BOOLEAN NOT NULL DEFAULT FALSE` — true for Docker auto-registered rows. Distinguishes "you can delete this" (false) vs. "deleting won't help, just unmount" (true and active).

`watched_files`: no changes.

No new tables.

## API surface

Existing `/api/watch/folders` CRUD stays as-is. Additions:

- `GET /api/watch/system` — `{platform, picker, watch_root}`. No auth changes; same auth as other watch routes.
- `GET /api/watch/folders/{id}/progress` —
  ```json
  { "total_files": 200,
    "completed_files": 190,
    "by_stage": {
      "extract": {"pending": 0, "running": 1, "done": 199, "error": 0},
      "ocr":     {"pending": 5, "running": 1, "done": 184, "error": 0},
      "chunk":   {"pending": 6, "running": 0, "done": 184, "error": 0},
      "entities":{"pending": 6, "running": 0, "done": 184, "error": 0},
      "embed":   {"pending": 6, "running": 0, "done": 184, "error": 0},
      "summarize":{"pending": 6,"running": 0, "done": 184, "error": 0},
      "finalize":{"pending": 6, "running": 0, "done": 184, "error": 0}
    },
    "scan_status": "scanning" | "idle",
    "last_scan_at": "2026-04-30T..." }
  ```
  `scan_status` is `"scanning"` while the watcher is doing an initial enumeration of the folder (just added, or daemon just started, or after re-mount). `"idle"` means the watcher is up and event-driven. `last_scan_at` is the timestamp of the most recent full enumeration pass. Backed by aggregation queries over `watched_files` joined to `ingestion_jobs` through `document_versions.version_id`. Joins simplify in Stage 3.
- `GET /api/watch/folders/stream` — SSE stream. Events fire when any folder's progress changes (debounced 500ms server-side). Payload `{folder_id, total_files, completed_files, scan_status}`. Frontend re-fetches the full progress object for the focused folder.

Semantic clarification on existing endpoints:

- `POST /api/watch/folders`:
  - On macOS: path must exist + be readable + be a directory.
  - On Docker: path must be a top-level subdir of `WATCH_ROOT`. User-supplied arbitrary paths via API are rejected to keep the model consistent.
- `DELETE /api/watch/folders/{id}`: returns 409 Conflict if `auto_discovered=true` AND `unavailable_reason IS NULL` (active Docker mount — must unmount first). Otherwise removes the folder row + cascades to `watched_files`. Does NOT delete `documents` or `chunks`; orphaned docs become unfoldered. Stage 2's onboarding wizard will surface the "what about my orphaned docs" prompt.

## Frontend

- Nav: `frontend/src/App.tsx` (or wherever the tab list is) replaces the "Upload" tab with a new "Folders" tab routed at `/folders`. The `/upload` route stays mounted for now (Stage 2 removes it).
- New page: `frontend/src/pages/FoldersPage.tsx`.
  - Lists folders from `/api/watch/folders`; reads platform shape from `/api/watch/system`.
  - Per row: display name, path, `auto_discovered` badge if Docker-mounted, status pill (`scanning` | `idle` | `unmounted` | `disabled`), progress bar, `190 / 200` counter.
  - Click row → expand inline: per-stage breakdown with `pending/running/done/error` counts.
  - Per-row actions: enable/disable toggle, edit display name (macOS only), delete (with confirm; disabled with tooltip when API would 409).
  - Top-right: "Add Folder" button on macOS (calls JS bridge); info bar with doc link on Docker.
- New hook `useFolderProgress()` subscribes to `/api/watch/folders/stream`. Updates summary counters from event payloads; re-fetches the focused folder's full progress on relevant events.
- Documents page: add a "Folder" column joined via `watched_files.folder_id → watched_folders.display_name`; empty for direct-uploaded docs. Add a "Folder: [All ▾]" filter chip — client-side filter only.

## Watcher behavior details

- New file in a watched folder → compute sha256 → upsert `watched_files (folder_id, relative_path, sha256, status='active')` → if doc didn't exist before, create `documents` + `document_versions` rows + enqueue `extract`. If a document with same sha256 already exists, link `watched_files.doc_id` to the existing doc (dedup).
- Modified file with changed sha256 → insert new `document_versions` row, link `watched_files.version_id` to it, enqueue `extract`. (Mechanics change in Stage 3.)
- Deleted file → `watched_files.status='removed'`, `removed_at=now()`. Documents stay queryable; existing 30-day soft-delete reaper handles eventual hard delete.
- Docker discovery loop (`discovery.py`): every 60s, list `WATCH_ROOT/*`. For new dirs, insert `watched_folders` with `auto_discovered=true`, `display_name=basename(path)`. For existing rows whose path no longer exists, set `enabled=false`, `unavailable_reason='unmounted'`. For existing rows whose path reappeared, clear `unavailable_reason` and re-enable (covers temp unmount/remount).

## macOS code retired

- Delete `macos/HarborClerkServer/HarborClerkServer/WatchedFolderManager.swift`.
- Delete the "Watched Folders" section in `PreferencesWindow.swift`.
- Delete any related Swift tests for FSEvents handling.
- Remove the now-unused folder-bookmark-data plumbing from `ServiceManager.swift` (env vars, etc.).
- Add a new `WatcherService` to `ServiceManager.swift` that launches `harbor-clerk-watcher` as a managed subprocess.

## Rollout

Single PR — schema migration + watcher daemon + new web page + Swift bridge change + Swift retirement. No feature flag, no dual-backend transitional period (no meaningful users today; YOLO).

## Testing

- **Unit**: `events.py` event-translation logic with synthetic `watchdog` events.
- **Integration (Python)**: spin up watcher against a `tmpdir`; create / modify / delete files; assert `watched_files` rows + ingestion_job enqueues. Forced-`PollingObserver` variant exercises the fallback path.
- **Integration (DB)**: insert a folder via the API, assert observer registers the new path within the `LISTEN/NOTIFY` debounce window.
- **Integration (Docker discovery)**: tmpdir mounted as `WATCH_ROOT`; create a subdir; wait one rescan cycle; assert auto-discovery row.
- **Frontend**: ESLint + tsc. No E2E (repo doesn't have one).
- **Manual smoke**: macOS — add folder via picker, drop a file, watch progress. Docker — `docker compose up`, mount a tmpdir, drop files, watch progress in same UI.

## Verification

End-to-end manual checks before merging:
- macOS: add folder via web UI, drop a PDF and a DOCX, both ingest end-to-end, both appear on Documents page with the folder column populated, progress UI updates live (no manual refresh).
- macOS: pause-resume watcher (via `ServiceManager`), re-drop files during pause, observer catches up after resume.
- Docker: bring up compose with two bind mounts under `WATCH_ROOT`; both folders appear in UI with `auto_discovered=true`; drop files in each, both ingest; remove one mount, that folder shows `unmounted` pill within one rescan cycle.
- Polling fallback: configure a folder backed by NFS or a fuse mount, assert ingestion still works (per-folder fallback log line confirms which observer is in use).

## Follow-ups not addressed here

- Stage 2 spec: watched-folder-only UX (onboarding wizard, remove direct-upload UI).
- Stage 3 spec: flatten document model (`document_versions` removal).
- Drop `watched_folders.bookmark_data` column once we're past one release cycle confidence.
- Install-path / dylib rpath cleanup (separate; see `project_install_path_revisit` memory note).
