# First-class Markdown, Obsidian & text-file handling — Design

- **Date:** 2026-05-22
- **Status:** Proposed (awaiting review)
- **Branch:** `feat/markdown-text-handling`

## Summary

Harbor Clerk currently treats Markdown as undifferentiated plain text and accepts a
narrow, hardcoded set of file extensions. This effort makes Markdown — and Obsidian
vaults specifically — a first-class data source, broadens text-format coverage, makes
chunking structure-aware, builds a wikilink graph, and surfaces what the watcher
silently drops. Delivered in five sequential phases on one branch.

## Background (verified current state)

- The extension allowlist is hardcoded and **duplicated**: `ALLOWED_EXTENSIONS` in
  `src/harbor_clerk/api/routes/uploads.py` and `_ALLOWED_EXTENSIONS` in
  `src/harbor_clerk/watcher/events.py`, kept in sync by a hand-written comment.
- `.md` goes through a direct UTF-8 decode (`_extract_txt` in
  `worker/stages/extract.py`). **Nothing in the codebase parses Markdown** — no parser
  dependency, no frontmatter handling, no heading detection.
- `document_headings` is **always empty for `.md`/`.txt`**: heading extraction is
  Tika-XHTML-only and explicitly skipped via `_SKIP_HEADINGS_EXTS` / `_SKIP_HEADINGS_MIMES`.
- `chunks` has a **single `chunk_text` column** that simultaneously feeds display, the
  `fts_en` / `fts_fr` generated columns, and the embedding. There is no raw-vs-indexed split.
- `_split_text` (`worker/stages/chunk.py`) is a flat sliding window — paragraph, then
  sentence, then word fallback. It is structure-blind; code fences get split mid-block.
- `kb_find_related` (`mcp_server.py`) is pure embedding similarity: average chunk vector
  → pgvector cosine distance, `min` per candidate doc.
- `documents.title` is set once to the filename stem at document creation and never
  updated by any pipeline stage. There is no JSON/metadata column on `documents`.
- The watcher's `_should_ignore` drops non-allowlisted files, dotfiles, and `__MACOSX`
  silently at DEBUG level. **No skip count is persisted anywhere.** `os.walk` in
  `watcher/main.py` already prunes dotted directories (so `.obsidian/` is correctly ignored).
- Alembic is a single rebased baseline `0001_initial`; the next migration is `0002_`.

## Goals

- Markdown headings populate `document_headings`, so `kb_document_outline` works for notes.
- Obsidian frontmatter and wikilinks become useful signal instead of index noise.
- A broad set of text formats ingest instead of being silently dropped.
- Chunk boundaries align with document structure.
- The watcher tells the user what it skipped.

## Non-goals

- Rendering Markdown to HTML.
- Format-specific handling for the newly-added extensions beyond plain-text parsing
  (deferred — see Future Work).
- Surfacing the wikilink graph in MCP tools or the UI (deferred — see Future Work).
- Resolving `![[embed]]` transclusions.

## Key decisions

**Wikilink scope: graph + retrieval.** Parse `[[wikilinks]]`, resolve them to corpus
documents, persist the graph in a new table, and feed it into `kb_find_related`.
Surfacing backlinks in MCP/UI is deferred.

**Markdown normalization: in-place, at extract time, no dual column.** `chunks` has one
text column; a raw-vs-indexed split would require a migration plus reworking two
generated FTS columns. Instead the extract stage produces lightly-normalized text that
feeds chunks/FTS/embedding/display alike. The untouched source file remains the true
raw (referenced in place; reachable via `revealInFinder`). Stripped Markdown is still
readable in citation snippets.

**Markdown parsing: `markdown-it-py`.** A CommonMark-compliant parser handles headings,
code-fence spans, and plain-text extraction with correct edge cases (e.g. a `#` inside a
fenced code block). Wikilinks (`[[...]]`), which are not CommonMark, are matched with a
regex pass. This matches the project's "AAA tools" philosophy and avoids a brittle
hand-rolled structural parser.

**YAML frontmatter: `pyyaml`.** Lightweight and ubiquitous; already used in the
test-corpora tooling.

**Extension routing: all new text formats take the plain-text path.** The deferred
reevaluation task decides which need format-specific handling later.

---

## Phase 1 — Extension coverage

**1a. De-duplicate the allowlist.** New module `src/harbor_clerk/file_types.py` with no
imports from the API or worker layers (so both the API and the watcher can import it
without a transitive chain). It exports:
- `ALLOWED_EXTENSIONS: frozenset[str]` — the full allowlist.
- `PLAIN_TEXT_EXTENSIONS: frozenset[str]` — extensions routed to the direct UTF-8 path.
- `MARKDOWN_EXTENSIONS: frozenset[str]` — `{".md", ".markdown"}`; get the full Markdown treatment.
- Category sets for readability (documents, spreadsheets, images, etc.).

`uploads.py` and `watcher/events.py` import from it; their local copies are deleted.

**1b. Add extensions.** Append to the allowlist: `.markdown .tsv .srt .vtt .rst .org
.adoc .tex .py .json .yaml .yml .toml .xml .log .ipynb .canvas`. All are added to
`PLAIN_TEXT_EXTENSIONS`. `.markdown` additionally joins `MARKDOWN_EXTENSIONS`.

**1c. Extraction routing.** In `worker/stages/extract.py`, the plain-text branch's
condition changes from `obj_key.endswith((".txt", ".md", ".csv"))` to a membership test
against `PLAIN_TEXT_EXTENSIONS`. `_NEVER_OCR_EXTS` gains all new text extensions. Plain-text-routed files never reach
the Tika heading path, so `_SKIP_HEADINGS_EXTS` needs no change for them; Markdown
headings come from the new parser (Phase 2), not Tika.

**1d. Excalidraw guard.** Excalidraw notes are saved as `*.excalidraw.md` and contain a
large compressed-JSON blob. `_should_ignore` (watcher) and the API ingest path skip any
path ending in `.excalidraw.md`. (`*.excalidraw` without `.md` is already dropped — not
on the allowlist.)

**Files:** `file_types.py` (new), `api/routes/uploads.py`, `watcher/events.py`,
`worker/stages/extract.py`.

---

## Phase 2 — Markdown structure extraction

A new module `src/harbor_clerk/worker/markdown_extract.py` holds the Markdown logic,
called from `extract.py`'s plain-text branch when the extension is in
`MARKDOWN_EXTENSIONS`. Processing order for a Markdown file:

1. **Decode** bytes (UTF-8, `errors="replace"` — unchanged).
2. **Frontmatter.** Detect a leading `---\n…\n---` block; parse it with `pyyaml`.
   - If it contains `title`, update `documents.title` (the extract stage gains a write to
     `doc.title` — currently no stage does this).
   - Flatten the remaining scalar/list fields into a short readable preamble
     (e.g. `Tags: project, finance. Aliases: Q3 Report.`) prepended to the body so the
     metadata stays searchable without YAML syntax noise.
   - The raw `---…---` block is removed from the body.
3. **Parse structure** with `markdown-it-py`: collect headings (ATX `#`–`######` and
   setext) with level + text, and the character spans of fenced code blocks.
4. **Normalize** (in place): strip ATX `#` markers (keep heading text), unwrap
   `[text](url)` → `text`, `[[target|alias]]` → `alias` (or `target`), strip emphasis
   markers, reduce `#tag` → `tag`. **Text inside fenced code blocks is left verbatim.**
5. **Headings → `document_headings`.** Write rows with `level`, `title`, `position`
   (character offset in the final normalized text), `page_num`.
6. **Paginate** the normalized text via the existing `_paginate_text`.

The result feeds the existing pipeline unchanged: chunks, FTS, embeddings, NER, and
summaries all see clean text. This fixes the known `classify_doc_type` misclassification
and the "extractive summary is just YAML frontmatter" bug.

**Files:** `worker/markdown_extract.py` (new), `worker/stages/extract.py`,
`pyproject.toml` (`markdown-it-py`, `pyyaml`).

---

## Phase 3 — Heading-aware chunking

`_split_text` / `run_chunk` in `worker/stages/chunk.py` gain two behaviors:

- **Heading boundaries are preferred break points.** When choosing where a chunk ends,
  a `document_headings.position` that falls inside the target window outranks the
  paragraph / sentence / word fallbacks. Chunks then tend to align with sections.
- **Code fences are never split.** For Markdown documents, fenced code-block spans
  (carried over from Phase 2) are treated as atomic — the splitter will not cut inside one.

This applies to all documents (Tika docs already have headings; Markdown docs now do
too). The existing target size / overlap / page-range / char-offset behavior is
preserved.

**Files:** `worker/stages/chunk.py`.

---

## Phase 4 — Wikilink graph

**New table `document_links`** (migration `0002`):

| Column | Type | Notes |
|---|---|---|
| `link_id` | UUID PK | |
| `src_doc_id` | UUID FK → documents, CASCADE, indexed | the document containing the link |
| `target_doc_id` | UUID FK → documents, CASCADE, nullable, indexed | NULL = unresolved |
| `link_text` | Text | raw inner text of `[[…]]` |
| `target_title` | Text, indexed | parsed note name (before `#` / `|`) |
| `anchor` | Text, nullable | the `#heading` portion, if any |
| `alias` | Text, nullable | the `|alias` portion, if any |
| `resolved` | Boolean, default false | |
| `created_at` | timestamptz | |

**Capture (extract stage).** While parsing Markdown (Phase 2, before normalization
rewrites `[[…]]`), a regex pass extracts wikilink targets. The extract stage deletes any
existing `document_links` rows for the doc (idempotent reprocess) and writes new rows
with `target_doc_id = NULL, resolved = false`.

**Resolution (finalize stage).** `finalize` resolves links against the current corpus:
- Match `target_title` to a document by `canonical_filename` stem (case-insensitive),
  falling back to `documents.title`. Obsidian links address notes by name.
- On a unique match, set `target_doc_id` and `resolved = true`. Ambiguous matches
  (two documents, same name) stay unresolved.
- Finalize also re-resolves **dangling links pointing at the doc being finalized** — it
  looks up unresolved `document_links` rows whose `target_title` matches this document's
  name and resolves them. This keeps the graph correct as a vault is ingested
  incrementally. (Limitation: a link is not retroactively resolved if its target is
  added much later without either document being reprocessed — acceptable for v1.)

**Retrieval integration.** `kb_find_related` blends the link graph with the existing
embedding similarity: documents that this document links to (or that link to it) via
resolved `document_links` rows are included in the result set and receive a similarity
boost so explicit links rank alongside or above pure-vector neighbors. The exact
blending (boost factor, dedup) is an implementation detail for the plan.

**Files:** `models/document_link.py` (new), `alembic/versions/0002_*.py` (new),
`worker/markdown_extract.py`, `worker/stages/extract.py`, `worker/stages/finalize.py`,
`mcp_server.py`.

---

## Phase 5 — Skipped-file hygiene

**Track skips.** `watched_folders` gains two columns (migration `0002`):
`skipped_count` (Integer, default 0) and `skipped_extensions` (`ARRAY(Text)` — the
distinct unsupported extensions encountered, following the existing
`ocr_languages_used` pattern).

During `_scan_folder` (`watcher/main.py`), files rejected for an unsupported extension
are counted and their extensions collected; the per-folder summary is written to
`watched_folders`. Dotfiles and `__MACOSX` are excluded from the count (they are
intentional noise, not "files the user might expect ingested").

**Surface it.** `_folder_to_dict` in `api/routes/watch.py` returns `skipped_count` and
`skipped_extensions`. `FoldersPage.tsx` shows a small muted line per folder when
`skipped_count > 0`: e.g. *"3 files not ingested — unsupported types: .canvas,
.excalidraw"*. This matches the project's "graceful degradation with nudges" philosophy.

**Files:** `models/watched.py`, `alembic/versions/0002_*.py`, `watcher/main.py`,
`api/routes/watch.py`, `frontend/src/pages/FoldersPage.tsx`.

---

## Data model changes

A single migration `0002_markdown_and_watch_skip` (following the `0001_initial`
numbering convention) adds:
- the `document_links` table (Phase 4), and
- the `watched_folders.skipped_count` / `skipped_extensions` columns (Phase 5).

No changes to `chunks`, `document_headings`, or `documents` schemas — the in-place
normalization decision specifically avoids a `chunks` migration. The extract stage's new
write to `documents.title` uses the existing column.

## Dependencies

Two additions to `pyproject.toml`: `markdown-it-py` (CommonMark parsing) and `pyyaml`
(frontmatter). Both are small, pure-Python, and widely used.

## Testing strategy

- **Unit tests** for each new parser: frontmatter detection/parsing, heading extraction
  (ATX + setext), normalization (including code-fence preservation), and wikilink
  extraction (`[[Note]]`, `[[Note#heading]]`, `[[Note|alias]]`).
- **Pipeline-stage tests** for the Markdown extract path, heading-aware chunking, and
  wikilink capture + resolution (including incremental/dangling-link resolution).
- **Allowlist / routing tests:** every new extension is accepted and routed to the
  plain-text path; `*.excalidraw.md` is skipped.
- **An Obsidian-vault fixture** — a small folder with frontmatter, wikilinks, headings,
  a fenced code block, an `.excalidraw.md` file, a `.canvas` file, and a `.obsidian/`
  directory — exercised end-to-end.
- Follows existing patterns in `tests/` (pytest, pgvector service container).

## Build order

Phase 1 → 2 → 3 → 4 → 5. Phase 3 depends on Phase 2 (headings). Phase 4's capture step
shares the Phase 2 Markdown-parsing module. Phase 5 is independent but shares migration
`0002` with Phase 4, so it lands after Phase 4.

## Deferred / future work

- **Reevaluate the newly-added extension list** — decide which formats warrant
  format-specific handling rather than plain-text parsing: `.srt` / `.vtt` subtitle
  timestamp stripping, `.ipynb` / `.json` / `.canvas` JSON-structure extraction, `.tex`
  de-macroing, `.xml` → Tika. *(This is the explicitly-requested follow-up task.)*
- **Surface the wikilink graph** in MCP (a `kb_backlinks` tool or backlink data in
  `kb_get_document`) and a backlinks panel in the document-detail UI.
- **`![[embed]]` transclusion** resolution.
- **Obsidian `.canvas`** text-node extraction from the Canvas JSON.
