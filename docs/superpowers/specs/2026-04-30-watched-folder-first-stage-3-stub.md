# Watched-Folder-First Refactor — Stage 3 Stub

> **Status:** brainstorm only — explicitly deferred until we get there.
> **Predecessors:** Stages 1 and 2 are not strict prerequisites — Stage 3 can ship in any order. But the user-facing payoff is largest after the watched-folder-only world (Stage 2) is in place.

## Goal

Eliminate the `document_versions` concept now that watched folders are the only user-facing ingest path and "a new version of an existing doc" is no longer a meaningful first-class operation.

## What's at stake

In a watched-folder-only world, a file changing on disk is just... the file changing. There's no semantic notion of "version 1 vs version 2" the way there was when users explicitly chose "new version of existing" in the upload flow. The `watched_files` row already tracks `(folder_id, relative_path)` and updates `version_id` and `sha256` when content changes. The version concept is now redundant.

## Tables that key off `version_id` today

Confirmed via `grep -rn "version_id" src/harbor_clerk/models/`:

- `ingestion_jobs.version_id` (FK)
- `document_pages.version_id` (FK)
- `chunks.version_id` (FK) — primary retrieval table; FTS + embedding live here
- `document_headings.version_id` (FK)
- `entities.version_id` (FK)
- `watched_files.version_id` (FK)
- `uploads.version_id` (FK, nullable)
- `documents.latest_version_id` — the doc → version pointer

Eight references. Removing `document_versions` re-keys all of them to `doc_id` and folds pipeline state up to the `documents` row.

## The deferred design decision

> "Let's dig into the pros and cons of changing the data model (vs just the UI) for #3 when we get there."

Two paths to consider when the Stage 3 spec is written:

**A. Full data model flatten (drop `document_versions`).** Re-key all 8 references, write a destructive migration that picks one version per doc to keep (likely `latest_version_id`), drop the table, retire the version-turndown UI, fold pipeline status into the document row. Highest payoff (clean schema, simpler queries, simpler MCP responses) but biggest implementation risk and irreversible without a backup.

**B. UI-only flatten (keep schema, hide versions).** Keep `document_versions` and all FKs. Stop creating multiple versions in the new code path: when a watched file changes, overwrite the existing version's data in place (or create a new one and immediately delete the old). UI shows the doc as flat — no version turndown. Lower risk; can ship without a destructive migration; preserves the option to bring versioning back later as a power feature. Cost: the schema retains a now-vestigial table and FKs that confuse new contributors.

Open question for the Stage 3 spec: which path? My (Claude's) gut: **B first, A only if we still want to bother once B is shipped.** The schema-flatten payoff is mostly aesthetic at that point.

## Decisions already locked in (during 2026-04-30 brainstorm)

- Stage 3 is its own spec → plan → implementation cycle. Not bundled with Stage 1 or 2.
- "Pulling pipeline info up to the main document view" is in scope — the version turndown UI goes away regardless of which schema path (A or B) we take.
- Migration story for legacy data: same as Stage 2 — no special handling. Whatever happens in the destructive migration applies uniformly.

## Scope (for the Stage 3 spec when it gets written)

Depends on A vs B choice:

**Path A (data model flatten) scope:**
- Schema migration: drop `document_versions`, alter all 7 child FKs to `doc_id`, drop `documents.latest_version_id`.
- Pre-migration data step: pick one version per doc (likely `latest_version_id`), null-out child rows for any other versions or delete them.
- Update every model in `src/harbor_clerk/models/` that has a `version_id` field.
- Update every query in `src/harbor_clerk/`, `tests/`, MCP tools, API routes that joins or references `version_id`.
- Update every MCP tool response shape that returns version metadata (`kb_get_document`, `kb_read_document`, `kb_ingest_status`, etc.).
- Update worker pipeline (`src/harbor_clerk/pipeline.py` and friends) to operate on `doc_id` directly.
- Frontend: remove `DocumentDetailPage` version turndown, fold pipeline status / extracted_chars / OCR conf etc. up to the doc row.
- macOS Swift: any references to version IDs in service plumbing.

**Path B (UI-only flatten) scope:**
- No schema migration. No model changes.
- Watcher event handler (`src/harbor_clerk/watcher/events.py`) changes: on modified file with new sha, instead of creating a new `DocumentVersion`, overwrite the existing one's `source_path` and clear/re-enqueue child rows (`document_pages`, `chunks`, etc.) for re-processing. Or: create a new version, then atomically delete the old one before queries can race.
- Frontend: remove the version turndown UI; pipeline status surfaces on the doc row.
- MCP tools: cap responses at one "version" per doc — don't return version arrays.

## Open questions for the Stage 3 spec

- **A or B?** This is the headline question. Don't pick until we get there.
- If A: what's the reprocess strategy when a watched file changes? Re-running the pipeline in place mutates `chunks` rows that may currently be returned in a search hit. Need a transactional rebuild path that doesn't show users half-rebuilt state.
- If B: how do we present pipeline status in the UI when there's only ever one current "state"? Particularly during reprocessing — show "extracting..." on the doc itself? A spinner overlay on the row?
- What does `kb_ingest_status` return after this? Today it's keyed off version. Probably becomes per-document.

## Why this is independent of Stages 1 and 2

Nothing in Stage 1 or 2 introduces a new dependency on `document_versions` shape. The watcher events module from Stage 1 explicitly creates new versions on content change today, but that's an isolated function we'd update in Stage 3 regardless. The user-facing UX is the same in either schema.

The reverse is also true: Stage 3 can ship before Stages 1 and 2 if we wanted (it just feels weird to flatten the model while users still have the upload UI in front of them).
