# Stage 3 release notes — what changed for callers

**Released:** 2026-05-02 (PR #255 merged 2026-05-01)
**Companion PRs:** #256 (finalize fix), #257 (Tika 422 diagnostic), #258 (TMPDIR fix), #259 (maintenance pass)

Stage 3 of the watched-folder-first refactor flattened the document model. This is a one-way schema change with caller-visible effects. If you maintain anything that talks to the Harbor Clerk REST API, MCP endpoint, or Postgres database directly, please read this before upgrading.

## TL;DR

- `document_versions` table is gone. Each document is a single row in `documents`.
- All `version_id` references in the API and MCP responses are gone — use `doc_id` everywhere.
- "New version of an existing document" semantics are dropped. Re-uploading a file with the same name replaces in place; cross-path SHA dedup is gone.
- `pipeline_seq` is the new race-protection mechanism. If you write a tool that mutates documents concurrently with the watcher or worker, you need to be aware of it.

## Schema changes

### `documents` table grew

Columns pulled up from `document_versions`:

- `sha256` (BYTEA, NOT NULL)
- `pipeline_status` (enum, NOT NULL) — note the underlying Postgres enum type is still named `version_status`; cosmetic only, will be renamed in a future migration
- `pipeline_seq` (INTEGER, NOT NULL, default 0) — incremented on every content replacement; race-protection signal
- `summary`, `summary_model`, `doc_type` (TEXT)
- `mime_type`, `source_path`, `error` (TEXT)
- `original_bucket`, `original_object_key` (TEXT) — actual storage location
- `has_text_layer`, `needs_ocr` (BOOLEAN)
- `extracted_chars`, `size_bytes` (BIGINT)

### Tables/columns removed

- `document_versions` table — dropped entirely
- `documents.latest_version_id` — dropped
- `version_id` column on `chunks`, `entities`, `document_pages`, `document_headings`, `ingestion_jobs`, `watched_files`, `uploads` — all dropped
- All child tables now key on `doc_id` only

### Migration 0017 caveat

If you are upgrading a non-trivial deployment that has Documents with `latest_version_id IS NULL` (rows from pre-extract crashes, or documents created via a deleted-only path), the migration's NOT-NULL conversion at step 7 will fail loudly. There is no auto-cleanup; you need to either delete those orphan rows manually or extend the migration to skip them before applying:

```sql
-- Inspect first
SELECT doc_id, title, created_at FROM documents WHERE latest_version_id IS NULL;

-- Delete if confirmed orphan
DELETE FROM documents WHERE latest_version_id IS NULL;
```

This is forward-defensive only — a precondition guard in the migration itself is intentionally deferred (modifying an applied migration carries its own risk).

## REST API changes

### Removed fields

These are no longer present in any API response:

- `version_id` — anywhere it appeared
- `versions[]` array on document responses
- `latest_version_id`, `latest_version_status`, `version_count` on document summaries
- The legacy `/api/docs/{doc_id}/versions/` namespace (if any tooling still hits these, expect 404)

### Modified semantics

- **`POST /api/uploads/confirm` with `action: "new_version"`**: still accepts the action name for back-compat, but now means "replace the existing document's content in place". `pipeline_seq` is incremented on the existing document, child rows are reset, ingestion runs fresh. There is no version history kept.
- **Cross-path SHA dedup is gone.** Uploading the same file from two different paths now produces two separate documents (each at its own watched-folder path). Watched folder is the source of truth; the file's location *is* its identity.
- **`document.error` and `document.pipeline_status`** are now top-level fields (previously on `latest_version`).

### Storage key path

Originals now live at `originals/docs/<doc_id>/<filename>` (was `originals/versions/<version_id>/<filename>`). The pre-existing rename migration handles legacy keys; PR #259 added a second pass for any keys written under the bug-era path `versions/<doc_id>/<filename>` (a regression in PR #255 that landed before the maintenance pass).

If you wrote integrations against the old key scheme, switch to reading `Document.original_object_key` rather than constructing keys yourself.

## MCP changes

All 16 MCP tools have been updated. The breaking changes:

- Responses no longer carry `version_id` fields
- `kb_get_document` no longer returns a `versions[]` array — flat metadata only
- `kb_list_recent` no longer carries `version_count`
- Tool docstrings (which LLM clients see) have been swept to remove "version", "all versions", "latest version", "version count" terminology

If you have an MCP client that parsed `version_id` to look up a specific version, that's no longer meaningful — there are no historical versions to address. Use `doc_id` for everything.

## Race protection: `pipeline_seq`

This is new and matters if you build tooling that mutates document state concurrently with the worker or watcher.

**The contract:** every time the document's content changes (re-upload, watcher modify event, manual reprocess), `documents.pipeline_seq` increments by 1. Workers read `pipeline_seq` at the start of a stage, do their work, and re-check before committing. If the seq has bumped, they abort silently — the new pipeline run owns the document now.

**For external mutations:**
- If you write a script that sets `documents.error = NULL` and re-enqueues an ingestion job, you should also bump `pipeline_seq` so any in-flight worker correctly aborts.
- If you mutate `Document` rows directly, do NOT touch `pipeline_seq` unless you intend to invalidate in-flight work.
- The previously-undocumented `cancel_version_jobs` Python alias has been removed; use `cancel_doc_jobs` instead.

**Atomicity note:** the current implementation does a `SELECT pipeline_seq` then an `UPDATE`, not an atomic `UPDATE ... WHERE pipeline_seq = :worker_seq`. The TOCTOU window is small but non-zero. The headline `mark_stage_done` race that defeated this protection in the original Stage 3 merge is closed (PR #259). A fully-atomic refactor of the stage write path remains future work.

## Frontend changes

If you embed Harbor Clerk's React frontend or fork it:

- `latest_version_status` field references in `DocumentsPage.tsx` and `ExplorePage.tsx` have been removed
- `StatusBadge` now takes `doc.status` directly
- The "New Version" UI affordance from the Document detail page has been removed (versions are no longer modeled)

## Worker / pipeline changes

If you wrote custom worker stages or extended the pipeline:

- `mark_stage_done` and `mark_stage_running` now accept an optional `worker_seq: int` keyword argument
- New stages should load `doc.pipeline_seq` early and pass it through to both functions, mirroring the existing stages (`worker/stages/*.py`)
- `embed.py` no longer opts out of `check_pipeline_seq` — every stage should participate

## Ecosystem checklist

If you maintain any of the following, this is your migration list:

- [ ] CLI tools that called `/api/docs/{doc_id}/versions/...` or referenced `version_id` — update to use `doc_id`
- [ ] Integration tests that fixture `DocumentVersion` ORM objects — switch to `Document` directly
- [ ] MCP transcripts archived from before Stage 3 — `version_id` references are no longer resolvable
- [ ] Direct SQL queries against `document_versions`, `chunks.version_id`, `*.version_id` — these all fail now
- [ ] Custom worker stages — thread `worker_seq` through `mark_stage_done` calls
- [ ] Backup/restore scripts that compute storage keys — read `Document.original_object_key` instead
- [ ] Monitoring dashboards that count "versions per document" — that metric no longer exists; everything is 1

## Companion docs

- `docs/superpowers/specs/2026-05-01-watched-folder-first-stage-3-design.md` — full Stage 3 design spec
- `docs/superpowers/plans/2026-05-01-watched-folder-first-stage-3.md` — implementation plan
- `docs/superpowers/specs/2026-05-02-language-packs-on-demand-design.md` — companion future-work spec for non-English language support (not yet implemented)
