# Watched-Folder-First Refactor — Stage 3 Design

## Context

Stages 1 and 2 brought watched folders to Docker, replaced the Swift FSEvents watcher with a Python `watchdog` daemon, and retired the direct-upload web UI. The data model still carries a `Document → DocumentVersion` shape inherited from the original "user uploads contract.pdf, then later uploads a revised contract.pdf" mental model.

In a watched-folder-only world, that shape doesn't earn its keep. When a file's content changes on disk, the previous bytes are gone — watched files are never copied to MinIO; the disk path IS the original. So old `DocumentVersion` rows point at:

- A `source_path` whose file is now different
- An `original_sha256` of bytes nothing on the system can produce
- `original_bucket` and `original_object_key` that are NULL (watched files don't store originals)
- Stale chunks/entities/pages/headings keyed by the old `version_id`

Pure metadata ghosts. The "version history" the data model promises is unreachable — there's no way to surface, query, restore, or even meaningfully describe a non-latest version. The cost is real: ~20 `Document.latest_version_id == X.version_id` joins on the read path, a 6-branch state machine in `events.py`, and a class of bugs that includes the empty-file SHA collision (PR #252) and likely more we haven't surfaced.

This stage flattens the model: pull the per-content state up to `documents`, drop `document_versions`, re-key all child tables to `doc_id`, and simplify the watcher's modify path to "DELETE old child rows, re-ingest." Doc identity (and therefore chat citations, conversation references, search-result links) survives content changes.

Stage 3 is independent of Stages 1 and 2; it could ship first if we'd ordered things differently.

## Goals

- Remove `document_versions` and the version concept from schema, code, and UI.
- Re-key all child tables (`chunks`, `entities`, `document_pages`, `document_headings`, `ingestion_jobs`, `watched_files`, `uploads`) to `doc_id`.
- Pull per-content state (sha, pipeline status, summary, format metadata, storage pointers) up to `documents`.
- Replace versioned modify semantics with replace-in-place: when a watched file's content changes, the doc gets re-ingested under the same `doc_id`.
- Drop cross-path SHA dedup. Each watched file path is its own document.
- Eliminate the bug class that produced PR #252 (cross-content dedup keyed on a non-discriminating fingerprint).

## Non-goals

- Removing the `Upload` ORM model or `uploads` table (Stage 2 lock — kept for non-user-facing sources like future email ingestion).
- Folder-as-query-scope (folds into the future Projects/Collections work, #97).
- Migrating MinIO contents that aren't reachable from any current Document (orphan cleanup is separate).
- Anything frontend-design-y. This stage is structural; no new visual treatments beyond the natural cleanup that falls out of removing the version wrapper.
- Preserving prior version data. The migration is destructive: once it lands, non-latest version rows and their child rows are gone.

## Architecture

### Data model

**Before:**

```
Document (doc_id, title, canonical_filename, latest_version_id, status, topic_id)
  └── DocumentVersion (version_id, doc_id, sha256, status, summary, has_text_layer,
                        needs_ocr, extracted_chars, source_path, mime_type,
                        size_bytes, summary_model, doc_type, error,
                        original_bucket, original_object_key)
        ├── DocumentPage (version_id, page_num, ...)
        ├── DocumentHeading (version_id, ...)
        ├── chunks (version_id, doc_id, ...)
        ├── entities (version_id, doc_id, ...)
        └── ingestion_jobs (version_id, stage, status, ...)
WatchedFile (folder_id, relative_path, sha256, doc_id, version_id, status)
Upload (..., doc_id, version_id)
```

**After:**

```
Document (doc_id, title, canonical_filename, status, topic_id,
          sha256, pipeline_status, pipeline_seq, summary, summary_model,
          doc_type, mime_type, source_path, has_text_layer, needs_ocr,
          extracted_chars, size_bytes, original_bucket, original_object_key,
          error)
  ├── DocumentPage (doc_id, page_num, ...)
  ├── DocumentHeading (doc_id, ...)
  ├── chunks (doc_id, ...)
  ├── entities (doc_id, ...)
  └── ingestion_jobs (doc_id, stage, status, ...)
WatchedFile (folder_id, relative_path, sha256, doc_id, status)
Upload (..., doc_id)
```

**Two new columns on `documents` warrant explanation:**

- `pipeline_status` — matches the existing `VersionStatus` enum (queued/extracting/ocring/chunking/embedding/summarizing/finalized/error). Distinct from the existing `status` (lifecycle: active/removed). Doc lifecycle and pipeline progress are orthogonal: an `active` doc can be `pipeline_status=embedding`, a `removed` doc can be `pipeline_status=finalized`.
- `pipeline_seq` — monotonic int, incremented on every reprocess. Workers read it at job start, compare-and-write at result time. If they lose a race against a content change, their write detects the seq bump and aborts cleanly. Prevents a stale in-flight extract from overwriting fresh content.

### Watcher refactor (`src/harbor_clerk/watcher/events.py`)

The 6-branch state machine collapses to 4:

| Existing state | SHA matches? | Action |
|---|---|---|
| (none) | n/a | Create Document. Set `pipeline_status=queued`. Enqueue `extract` job. |
| active | same | No-op. |
| active | different | Bump `pipeline_seq`. DELETE child rows for `doc_id`. Set `pipeline_status=queued`. Enqueue `extract` job. |
| removed | same | Resurrect: status `removed` → `active`. No re-ingest. |
| removed | different | Resurrect + re-ingest: status flip + bump seq + DELETE child rows + enqueue. |
| (deleted event) | n/a | If existing+active: status `active` → `removed`, set `removed_at`. |

The cross-content dedup branch (`SELECT DocumentVersion WHERE original_sha256 = ?`) is removed entirely. Two paths with identical content produce two independent docs. Storage cost: vectors get re-embedded for duplicates, ~1.5KB per chunk. The dedup-induced bug class (empty-SHA collision, plus any unsurfaced cousins) goes with it.

### Pipeline + worker refactor

All seven stage modules (`extract`, `ocr`, `chunk`, `entities`, `embed`, `summarize`, `finalize`) and the orchestrator in `pipeline.py` switch their primary key from `version_id` to `doc_id`. Job enqueue, fan-out (extract → chunk → entities/embed/summarize parallel → finalize), and the `LISTEN/NOTIFY` channels stay structurally identical — just rekeyed.

**Reprocess flow** (used by `kb_reprocess`, the maintenance reprocess endpoint, and the watcher's modify branch):

1. Bump `pipeline_seq`.
2. DELETE FROM chunks/entities/document_pages/document_headings WHERE doc_id = ?
3. DELETE FROM ingestion_jobs WHERE doc_id = ? (any in-flight jobs have a stale seq; their writes will abort)
4. Set `pipeline_status = queued`.
5. INSERT INTO ingestion_jobs (doc_id, stage='extract', status='queued').

**Race protection**: workers read `pipeline_seq` from the doc row at job start (let's call it `worker_seq`). At result-write time, they `UPDATE documents SET ... WHERE doc_id = ? AND pipeline_seq = :worker_seq`. If `rowcount == 0`, a content change beat them; the worker logs and exits without writing chunks/entities/etc. The new ingestion_job for the new content is already queued and will run.

### API + MCP

- `latest_version_id` field removed from all responses (`/api/docs`, MCP tools).
- `version_count` field removed from `/api/docs` (would always be 1).
- `Document.latest_version_id == X.version_id` joins in `documents.py`, `mcp_server.py`, `search.py`, `topics.py`, `passages.py`, `chat.py`, `research.py`, `llm/research.py` all become direct `documents.doc_id = X.doc_id` queries.
- MCP tools (`kb_search`, `kb_read_passages`, `kb_get_document`, `kb_document_outline`, `kb_find_related`, `kb_ingest_status`, `kb_reprocess`): wire-format compatible (drops a field). LLM clients re-discover the schema each call, so no version concept will leak through.
- Any `/api/docs/{doc_id}/versions/...` URL space found during the route audit becomes `/api/docs/{doc_id}/...` directly. The implementer greps `src/harbor_clerk/api/routes/` for `version_id` route segments and rewrites or removes each one — there are no external clients we'd break.
- `/api/jobs/stream` SSE event payload: `version_id` → `doc_id`.

### Frontend

**`DocumentDetailPage.tsx` — biggest single change.** The entire `VersionInfo[]` / `VersionBanner` / "Version 1 (date)" wrapper collapses. Everything currently rendered per-version moves up to the Document body:

- Ingestion-complete banner (driven by `pipeline_status`)
- Doc type / OCR / Text-layer / Chars stats line
- Source path
- Ingestion Jobs disclosure (stage / status / progress / time)
- Content section (already at doc level; just drops its version-keyed props)

The `versionsWithNumber`, `allVersionsReady`, `versionCount` machinery (lines 557-564 of the current file) all goes. Estimated 200-300 line cut.

**Other touched files (verified by grep):**

- `frontend/src/hooks/useJobEvents.ts` — SSE event payload shape (`version_id` → `doc_id`)
- `frontend/src/hooks/useQueueTray.ts` — list key + cross-event correlation
- `frontend/src/components/queue-tray/QueuePanel.tsx` — `key={item.version_id}` → `doc_id`
- `frontend/src/pages/SearchPage.tsx` — citation/result `version_id` references
- `frontend/src/pages/DocumentsPage.tsx`, `ExplorePage.tsx` — drop the `version_count` column

**API types**: replace `VersionInfo[]` on the doc detail response with flattened fields directly on the doc object. Inline types live in the page files (no central `types/` directory for these).

### Storage

Object key pattern changes:

- Before: `originals/versions/<version_id>/<filename>`
- After: `originals/docs/<doc_id>/<filename>`

A one-shot rename pass runs as part of the Alembic upgrade (or right after, in a Python data-migration step). It walks the existing rows in `documents` (post-backfill — so the latest-version key is already known) and renames `originals/versions/<latest_version_id>/<f>` to `originals/docs/<doc_id>/<f>` in MinIO/filesystem. Non-latest version objects are deleted (their parent rows are about to be deleted; nothing references them).

The watched-files-don't-store-originals asymmetry is preserved. Watched files have a disk path; uploaded files have a MinIO/filesystem object. Both reach the same flattened `documents` row from different ingestion entry points.

### Upload flow

The Stage 2 lock keeps `/api/uploads/*` and the upload-session machinery alive for non-user-facing sources (future email ingestion, automation, tests). With Stage 3:

- On confirm, the session creates a `Document` directly. No DocumentVersion is created. `pipeline_status=queued`, `extract` job enqueued.
- `uploads.version_id` column is dropped. `uploads.doc_id` stays for audit/correlation.
- `Upload` ORM model survives. The upload-flow tests are updated to reflect single-table creation.

## Migration

Single Alembic upgrade. Destructive — once it lands, non-latest version data is gone. Operationally: workers and the watcher must be stopped during the upgrade. The macOS native app's `make apps` install path already stops services on launch and runs migrations before starting them, so this is the standard install flow. Docker users: `docker compose down`, pull, `docker compose up`.

**Steps (single upgrade transaction where possible; data migration steps may need their own commits for batched UPDATE/DELETE):**

1. Add new columns to `documents` (all nullable for backfill):
   - `sha256` BYTEA
   - `pipeline_status` (existing `version_status` enum)
   - `pipeline_seq` INT NOT NULL DEFAULT 0
   - `summary`, `summary_model`, `doc_type`, `mime_type`, `source_path`, `error`, `original_bucket`, `original_object_key` TEXT
   - `has_text_layer`, `needs_ocr` BOOL
   - `extracted_chars`, `size_bytes` BIGINT
2. Add `doc_id` columns to `document_pages`, `document_headings`, `ingestion_jobs` (nullable for backfill).
3. Backfill: for each `Document`, copy DV columns of the row matching `latest_version_id` UP into `documents`.
4. Backfill child tables: populate the new `doc_id` columns from each row's `version_id` → DV's `doc_id`.
5. Delete non-latest data: `DELETE FROM chunks/entities/document_pages/document_headings/ingestion_jobs WHERE doc_id IS NOT NULL AND version_id != (SELECT latest_version_id FROM documents WHERE doc_id = X.doc_id)`.
6. Storage rename pass (Python): for each `Document` with `original_object_key IS NOT NULL`, copy the object from the old key to the new doc-keyed path; delete the old.
7. NOT NULL the new `doc_id` columns now that backfill is done.
8. Drop `version_id` columns from `chunks`, `entities`, `document_pages`, `document_headings`, `ingestion_jobs`, `watched_files`, `uploads`.
9. Drop `latest_version_id` from `documents`.
10. `DROP TABLE document_versions`.
11. Add new unique constraints: `(doc_id, chunk_num)` on chunks, `(doc_id, page_num)` on pages, `(doc_id, stage)` on ingestion_jobs (replacing their `version_id`-keyed equivalents).
12. Reuse the existing `version_status` Postgres enum for `documents.pipeline_status` (same value set: queued/extracting/ocring/chunking/embedding/summarizing/finalized/error). Don't rename — the cost is a slightly stale type name in the DB schema; the benefit is no enum recreation and no risk of dropping-it-while-still-referenced. The Python `VersionStatus` enum class can be aliased / renamed to `PipelineStatus` in a follow-up if anyone cares.

**`pipeline_seq` initialisation**: backfill sets all rows to 0. Workers compare-and-write from there; the first reprocess after upgrade bumps it to 1.

## Testing

- **Unit tests** for `events.py`'s collapsed state machine (4 cases × deleted-event = 5 cases). Existing tests in `tests/watcher/test_events.py` are restructured (still keeping the empty-SHA regression coverage; just moving from `_new_version_on_doc` assertions to "doc child rows wiped + reseq + extract enqueued").
- **Pipeline tests**: race protection — submit a stale-seq write, assert it's rejected by the rowcount check.
- **API tests**: existing `tests/test_api_documents.py` updated for the flattened response shape; `latest_version_id` and `version_count` assertions removed.
- **Migration test**: a fixture corpus with multi-version docs (one Document with two DocumentVersions, only one chunk per version), run the upgrade, assert child rows from the non-latest version are gone, fields backfilled, latest version's chunks/entities preserved.
- **Storage rename test**: mocked MinIO with two objects (latest + prior version), run rename pass, assert latest is renamed to doc-keyed path and prior is deleted.
- **Frontend**: lint / typecheck / build clean. No new component tests (project has no test infra). Manual smoke matters here.
- **Manual smoke checklist**:
  1. Fresh install (no corpus): add a watched folder, drop a PDF, watch ingest complete, query via chat. Expected: works as before, but DocumentDetailPage shows the flattened layout.
  2. Migration smoke: install on a system with existing multi-version docs (the user's corpus likely has some). Verify all docs land with a single flattened state, no orphan rows.
  3. Modify-in-place: edit a watched file, save. Watch the in-flight ingestion abort cleanly via `pipeline_seq` race protection. New ingestion completes. DocumentDetailPage shows new content; chat citations to the doc still resolve.

## Verification

- All existing pytest passes (with updates for the flattened shape).
- ruff clean.
- Frontend lint / typecheck / format / build clean.
- DocumentDetailPage no longer references `versions`, `version_id`, or "Version 1" anywhere.
- `grep -rn "version_id\|latest_version\|DocumentVersion\|document_versions" src/ frontend/src/ tests/` returns zero results outside the migration file (and the migration's deletion of those names).
- `kb_*` MCP tools end-to-end smoke: search returns citations, citations resolve, doc detail loads.

## Rollout

Single PR (large — code touch is broad even though scope is structurally focused). The migration is destructive; once merged, prior version data is gone. No phased deploy because there's only ever one instance.

Operationally on the user's Mac: pull, `make apps`, app restart picks up the migration on launch. ~15-30s expected for the migration on a corpus of ~700 docs (timing dominated by the storage rename pass).

## Follow-ups not addressed here

- **Decommissioning `/api/uploads/*` REST endpoints.** Stage 2 lock — revisit when email ingestion ships and we know whether anything else depends on them.
- **Removing `Upload` ORM model + `uploads` table.** Same — defer until the legacy direct-uploaded audit data is provably unused.
- **Folder-as-query-scope.** Folds into the Projects/Collections work (#97).
- **MinIO orphan cleanup beyond the rename pass.** Separate pass once we have observability on what's actually orphaned.
- **`document.status` casing consistency.** Today some code paths write `"active"`, one path in `llm/research.py` filters on `"ready"`. Out of scope for Stage 3 but worth a follow-up bug — likely a regression from a prior refactor.
