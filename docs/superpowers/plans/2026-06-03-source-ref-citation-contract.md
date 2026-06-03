# SourceRef and Citation Contract - Implementation Plan

> **For agentic workers:** This is a detailed implementation plan. Execute task-by-task, preserving backwards-compatible response fields while adding the new `source` object and `citation` string. Do not remove legacy fields in this rollout.

**Goal:** Add a central `SourceRef` builder and thread it through the highest-value REST, MCP, CLI, chat, and research citation paths so Harbor Clerk has one stable source/citation contract.

**Spec:** `docs/superpowers/specs/2026-06-03-source-ref-citation-contract-design.md`

**Release plan:** `docs/superpowers/plans/2026-06-03-release-readiness-action-plan.md`

**Architecture:** A new pure builder module formats document, email, attachment, page, section, folder-label, and relative-path information into a structured `SourceRef` object. Thin async context loaders bulk-fetch watched-folder and parent-email data so routes/tools do not N+1 query. Existing response shapes keep their legacy top-level fields, but gain `source` and `citation`. LLM citation extraction prefers `source` when present and falls back to legacy fields.

**Non-goals for this plan:**

- No path-disclosure API-key scope yet.
- No absolute paths in MCP/CLI/cloud-visible payloads.
- No removal of legacy `doc_id`, `doc_title`, `pages`, `page_start`, or `page_end` fields.
- No full UI citation-chip rewrite beyond adding types/compatibility hooks if needed.
- No verifier/citation-support UI.

**Research/verifier decision:** Research citations normalize later, while preserving verifier compatibility. In practice: new research citation records may gain `source`, `citation`, and `pages`, but the existing `doc_title` and `page` fields must remain until the verifier loop is deliberately migrated. Do not break `_verify_citations` as part of the SourceRef rollout.

---

## Current State Snapshot

- REST `POST /api/search` returns `SearchHitOut` with top-level `chunk_id`, `doc_id`, `doc_title`, `page_start`, `page_end`, and no citation string.
- REST `POST /api/passages/read` returns `PassageDetail` with similar top-level fields and no `source`.
- MCP builds result dicts inline in `src/harbor_clerk/mcp_server.py`.
- CLI JSON passes MCP output through; text rendering builds its own title/page line.
- `src/harbor_clerk/llm/citations.py` extracts best-effort citation records from several ad hoc tool-result shapes.
- `tests/test_mcp_metadata_filter.py::test_kb_get_document_does_not_leak_source_path` already pins a no-host-path rule for MCP document metadata.
- Watched-folder source identity lives in `watched_files.relative_path` plus `watched_folders.display_name`/`path`.
- Email identity usually lives in `Document.email_*` columns. Tika fallback metadata may exist under `doc.doc_metadata["tika"]["email_*"]`. Attachments link to parent email via `Document.email_parent_doc_id`.

---

## Contract

Every new `source` object should have this shape, omitting null values on JSON output where practical:

```json
{
  "doc_id": "uuid",
  "doc_title": "string",
  "chunk_id": "uuid",
  "pages": "4-5",
  "section": "string",
  "source_kind": "document",
  "source_label": "string",
  "folder_label": "string",
  "relative_path": "string",
  "citation": "Contract A, pp. 4-5"
}
```

Accepted `source_kind` values:

- `document`
- `email`
- `attachment`
- `unknown`

Compatibility rule:

- Add top-level `citation` as an alias of `source.citation`.
- Keep old top-level fields for now.

Path rule:

- Human REST document endpoints may keep existing local path fields.
- MCP and CLI result payloads must not include absolute paths.
- `folder_label` and `relative_path` are allowed by default.
- Future path-disclosure scope handles filename-only/relative/absolute modes.

---

## Task 1: Pure SourceRef builder

**Why:** Centralize citation formatting and source-policy decisions before touching any route/tool.

**Files:**

- Create: `src/harbor_clerk/source_ref.py`
- Create: `tests/test_source_ref.py`

### Step 1.1: Write builder tests first

Cover:

- Document citation with one page: `Contract A, p. 4`.
- Document citation with range: `Contract A, pp. 4-5`.
- Document citation without page: `Contract A`.
- Missing title falls back to canonical filename, then `Untitled document`.
- Email citation uses native metadata: `Email from Jane Doe, "Budget follow-up", Mar 7, 2025`.
- Email citation with missing sender still includes subject/date when present.
- `.eml` fallback uses Tika metadata if native `email_*` fields are missing.
- `.eml` fallback uses file/title metadata if email parsing failed.
- Attachment citation includes attachment filename/title and parent email identity.
- `source_label` and `citation` both exist and can differ.
- No field renders `"None"`, `"null"`, or empty quotes.
- No absolute path appears in `source.to_dict()`.

### Step 1.2: Add core dataclass and helpers

Implement:

```python
@dataclass(frozen=True)
class SourceRef:
    doc_id: str
    doc_title: str
    chunk_id: str | None = None
    pages: str | None = None
    section: str | None = None
    source_kind: Literal["document", "email", "attachment", "unknown"] = "document"
    source_label: str = ""
    folder_label: str | None = None
    relative_path: str | None = None
    citation: str = ""

    def to_dict(self) -> dict: ...
```

Helpers:

- `format_pages(page_start, page_end) -> str | None`
- `format_document_citation(title, pages) -> str`
- `format_email_citation(...) -> str`
- `format_attachment_citation(...) -> str`
- `build_source_ref(...) -> SourceRef`

Recommended `build_source_ref` signature:

```python
def build_source_ref(
    *,
    doc: Document | None,
    chunk_id: str | None = None,
    pages: str | None = None,
    section: str | None = None,
    watched_file: WatchedFile | None = None,
    watched_folder: WatchedFolder | None = None,
    parent_email_doc: Document | None = None,
    include_relative_path: bool = True,
) -> SourceRef:
    ...
```

Do not let this function query the database. Keep it pure.

### Step 1.3: Implement email fallback order

For email docs:

1. Native `Document.email_from_*`, `email_subject`, `email_date_sent`.
2. Tika metadata fallback under `doc.doc_metadata["tika"]`:
   - `email_from`
   - `email_subject`
   - `email_date`
3. File/title fallback:
   - `doc.title`
   - `doc.canonical_filename`
   - relative path leaf

Email detection:

- `doc.email_parent_doc_id is None` and any native email field exists.
- `doc.mime_type == "message/rfc822"`.
- `doc.canonical_filename` or `relative_path` ends with `.eml`.

Attachment detection:

- `doc.email_parent_doc_id is not None`.
- Use `parent_email_doc` when available to build parent email citation.

### Step 1.4: Implement path handling

Folder label:

- Prefer `WatchedFolder.display_name`.
- Else basename of `WatchedFolder.path`.
- Else `None`.

Relative path:

- Include `WatchedFile.relative_path` only when `include_relative_path=True`.
- Never derive a relative path by slicing `Document.source_path`; use watched-folder data.
- Never output `Document.source_path`.

### Step 1.5: Run tests

Run:

```bash
uv run pytest tests/test_source_ref.py -v
```

---

## Task 2: Bulk SourceRef context loader

**Why:** Routes/tools need watched-folder and parent-email context without repeating ad hoc joins.

**Files:**

- Modify: `src/harbor_clerk/source_ref.py`
- Create: `tests/test_source_ref_context.py`

### Step 2.1: Add `SourceRefContext`

Implement a small context object:

```python
@dataclass
class SourceRefContext:
    docs_by_id: dict[uuid.UUID, Document]
    watched_files_by_doc: dict[uuid.UUID, WatchedFile]
    watched_folders_by_id: dict[uuid.UUID, WatchedFolder]
    parent_email_docs_by_id: dict[uuid.UUID, Document]

    def ref_for_doc(...): ...
```

`ref_for_doc` should call the pure builder.

### Step 2.2: Add async loader

Implement:

```python
async def load_source_ref_context(
    session: AsyncSession,
    doc_ids: Iterable[uuid.UUID | str],
) -> SourceRefContext:
    ...
```

Behavior:

- Load `Document` rows for `doc_ids`.
- Load active `WatchedFile` rows for those docs.
- Load `WatchedFolder` rows for those watched files.
- Load parent email documents for any `doc.email_parent_doc_id`.
- Do not raise if watched tables are absent or missing rows; return partial context.

### Step 2.3: Add context tests

Cover:

- Folder display name and relative path included.
- Parent email doc loaded for attachment citation.
- Missing watched rows still produces a valid source.
- No absolute path appears in `ref.to_dict()`.

Run:

```bash
uv run pytest tests/test_source_ref.py tests/test_source_ref_context.py -v
```

---

## Task 3: REST search and passages

**Why:** Human Search is becoming the default surface and should get structured citations early.

**Files:**

- Create: `src/harbor_clerk/api/schemas/source_ref.py`
- Modify: `src/harbor_clerk/api/schemas/search.py`
- Modify: `src/harbor_clerk/api/routes/search.py`
- Create: `tests/api/test_search_source_ref.py`

### Step 3.1: Add Pydantic schema

Create `SourceRefOut` mirroring the dataclass:

```python
class SourceRefOut(BaseModel):
    doc_id: str
    doc_title: str
    chunk_id: str | None = None
    pages: str | None = None
    section: str | None = None
    source_kind: Literal["document", "email", "attachment", "unknown"]
    source_label: str
    folder_label: str | None = None
    relative_path: str | None = None
    citation: str
```

### Step 3.2: Extend search schemas compatibly

Add optional fields:

- `SearchHitOut.source: SourceRefOut | None = None`
- `SearchHitOut.citation: str | None = None`
- `PassageDetail.source: SourceRefOut | None = None`
- `PassageDetail.citation: str | None = None`

Do not remove existing fields.

### Step 3.3: Enrich `POST /api/search`

After `hybrid_search`, bulk-load source context for `result.hits` doc IDs.

Update `_hit_to_out` to accept `source_ref`:

- `pages = format_pages(h.page_start, h.page_end)`
- `chunk_id = h.chunk_id`
- `section = None` for first REST pass unless heading lookup is added.
- `citation = source_ref.citation`

### Step 3.4: Enrich `POST /api/passages/read`

After loading chunks/docs, use `SourceRefContext.ref_for_doc` for each passage.

The existing route already has chunk rows and doc rows; the only new DB work should be watched-folder/parent-email context.

### Step 3.5: REST tests

Cover:

- Search hit includes `source` and top-level `citation`.
- Legacy top-level fields still exist.
- Passage includes `source` and `citation`.
- Email search result has email-native citation.
- Attachment passage includes parent email context when available.
- MCP/cloud-visible path policy is not directly tested here, but REST search should still not add absolute paths to `source`.

Run:

```bash
uv run pytest tests/api/test_search_source_ref.py -v
```

---

## Task 4: MCP high-volume citation-bearing tools

**Why:** MCP is the main cloud/agent interface. Search/read tools should get `source` early.

**Files:**

- Modify: `src/harbor_clerk/mcp_server.py`
- Modify/create tests in `tests/test_mcp_tools.py` or `tests/test_mcp_source_ref.py`

### Step 4.1: Add source refs to `kb_search` and `kb_batch_search`

Modify `_format_search_response` to accept a source-ref map keyed by `(doc_id, chunk_id)`.

Each hit keeps:

- `chunk_id`
- `doc_id`
- `doc_title`
- `pages`
- `section`
- `score`
- `language`
- `text`

Each hit gains:

- `source`
- `citation`

Build the map in `kb_search` and each `kb_batch_search` query after `hybrid_search`.

### Step 4.2: Add source refs to `kb_find_all`

Each `results[]` row keeps current fields and gains:

- `source`
- `citation`

For `presentation="full"`, the row source should include `top_chunk_id` and `page_range` when available. If no chunk exists, emit a document-level source.

### Step 4.3: Add source refs to `kb_read_passages`

Each `passages[]` row gains:

- `doc_id` if not already present in the current MCP shape
- `source`
- `citation`

This is a useful small compatibility improvement: `llm/citations.py` currently has to keep chunk-only records from `kb_read_passages` because the MCP passage shape omits `doc_id`.

### Step 4.4: Add source refs to `kb_expand_context`

Each `chunks[]` row gains:

- `source`
- `citation`

The top-level document identity remains unchanged.

### Step 4.5: MCP tests

Cover:

- `kb_search` hit includes `source.citation` and top-level `citation`.
- `kb_batch_search` nested hits include source refs.
- `kb_find_all` rows include source refs.
- `kb_read_passages` includes `doc_id`, `source`, and `citation`.
- `kb_expand_context` chunks include source refs.
- No MCP response includes `Document.source_path`.
- No MCP response includes `/Users/` when source path is seeded as `/Users/alex/private/foo.pdf`.

Run targeted tests:

```bash
uv run pytest tests/test_mcp_tools.py tests/test_mcp_metadata_filter.py -k "source_ref or source_path or search or read_passages or find_all" -v
```

---

## Task 5: MCP document-row tools

**Why:** Agents often pivot from search to document-listing and document-metadata tools. Those rows should carry the same source identity.

**Files:**

- Modify: `src/harbor_clerk/mcp_server.py`
- Modify: `src/harbor_clerk/mcp_lookup_tools.py`
- Modify/create tests in `tests/test_mcp_tools.py`, `tests/test_mcp_lookup_tools.py`, or `tests/test_mcp_source_ref.py`

### Step 5.1: Add document-level source refs

Add `source` and `citation` to document rows returned by:

- `kb_get_document`
- `kb_list_recent`
- `kb_corpus_overview`
- `kb_document_outline`
- `kb_find_related`
- `kb_documents_by_date`
- `kb_verify_identifier` unique match and ambiguous candidates

Keep all current fields.

### Step 5.2: Consider `kb_entity_search`

If `kb_entity_search` returns per-mention `doc_id` and `chunk_id`, add source refs there too. If this becomes messy, defer it explicitly in the PR description and follow-up list.

### Step 5.3: Tests

Cover at least:

- `kb_get_document` includes `source` but still does not leak `source_path`.
- `kb_documents_by_date` result includes `source` and `citation`.
- `kb_verify_identifier` unique match includes `source`.
- `kb_find_related` related rows include `source`.

Run:

```bash
uv run pytest tests/test_mcp_lookup_tools.py tests/test_mcp_metadata_filter.py tests/test_mcp_tools.py -k "verify_identifier or documents_by_date or get_document or find_related or source_path" -v
```

---

## Task 6: LLM citation extraction compatibility

**Why:** Ask and Research already persist citations from tool results. Once tool results carry `source`, the extractor should preserve the richer object.

**Files:**

- Modify: `src/harbor_clerk/llm/citations.py`
- Modify: `tests/test_llm_citations.py`

### Step 6.1: Prefer `source` objects

In `_record`, if a result row has a dict `source`:

- Copy the `source` dict into the citation record.
- Populate legacy flattened keys from `source` when absent:
  - `doc_id`
  - `doc_title`
  - `chunk_id`
  - `pages`
  - `citation`
- Preserve numeric `score` from the result row.

Do not mutate the parsed tool result.

### Step 6.2: Keep legacy fallback

The extractor must continue to pass all existing tests for old shapes.

### Step 6.3: Add extractor tests

Cover:

- `kb_search` hit with `source` yields citation with nested `source` and flattened legacy keys.
- Top-level `citation` is preserved.
- Legacy hit without `source` still works.
- Dedup keeps richer `source` record when replacing by higher score.
- Chunk-only legacy records still survive.

Run:

```bash
uv run pytest tests/test_llm_citations.py -v
```

---

## Task 7: CLI output and help compatibility

**Why:** CLI is the shell-first agent surface. JSON should preserve `source`; text should prefer the formatted citation when available.

**Files:**

- Modify: `src/harbor_clerk/cli/output.py`
- Modify: `src/harbor_clerk/cli/help/search.txt`
- Modify: `src/harbor_clerk/cli/help/batch-search.txt`
- Modify: `src/harbor_clerk/cli/help/read-passages.txt`
- Modify/create tests in `tests/cli/test_output.py`

### Step 7.1: Update text renderer

For search text mode:

- Prefer `r["citation"]`.
- Else prefer `r["source"]["citation"]`.
- Else fall back to `doc_title` + `pages`.

Keep chunk id visible for agents/humans.

### Step 7.2: Update CLI help

Help should say JSON results include:

- `source`
- `citation`
- legacy citation-ready fields

Update any current "expecting a standalone citation field" caveats to reflect the new field.

### Step 7.3: CLI tests

Cover:

- Text search output uses `citation` when present.
- JSON rendering preserves nested `source`.
- Legacy payload still renders.

Run:

```bash
uv run pytest tests/cli/test_output.py tests/cli/test_commands_search.py -v
```

---

## Task 8: Research citation normalization

**Why:** Research citations currently persist as doc/page records built inside `_read_evidence`. They should converge toward SourceRef so future citation-support validation has structured inputs.

**Memorialized decision:** Research citations normalize later, while preserving verifier compatibility. This task is intentionally late in the plan because the verifier currently consumes `doc_title` and `page`; SourceRef must enrich that shape, not replace it, until a separate verifier migration exists.

**Files:**

- Modify: `src/harbor_clerk/llm/research.py`
- Modify: `src/harbor_clerk/api/routes/research.py` if read-time compatibility is needed
- Modify: `src/harbor_clerk/api/schemas/research.py` only if adding typed source schema is low-risk
- Modify: `tests/test_api_research_citations.py`
- Modify: `tests/test_research_verifier.py` only if verifier input shape changes

### Step 8.1: Update `_read_evidence`

Where it currently appends:

```python
{"doc_id": ..., "doc_title": ..., "page": ...}
```

append a richer record:

```python
{
  "doc_id": ...,
  "doc_title": ...,
  "page": ...,
  "pages": ...,
  "citation": source_ref.citation,
  "source": source_ref.to_dict(),
}
```

Keep `page` for verifier compatibility until that code is deliberately updated.

### Step 8.2: Preserve old persisted citations

`GET /api/research/{id}` must still handle old `research_state.citations` values that lack `source`.

Either:

- return old citations as-is, or
- enrich old citations best-effort when `doc_id` is present.

Prefer the simpler path unless UI needs the richer shape immediately.

### Step 8.3: Verifier compatibility

The verifier currently reads `doc_title` and `page`. Do not break it.

If changing verifier input, update tests in `tests/test_research_verifier.py`.

### Step 8.4: Research tests

Cover:

- New research citations can include `source` and `citation`.
- Old citations still round-trip.
- Dedupe does not drop `source`.

Run:

```bash
uv run pytest tests/test_api_research_citations.py tests/test_research_verifier.py -v
```

---

## Task 9: Frontend type hook, minimal

**Why:** Even if full citation-chip rendering is a later UI task, frontend code should have a stable type to consume.

**Files:**

- Create or modify: `frontend/src/types/sourceRef.ts`
- Modify: `frontend/src/pages/SearchPage.tsx` only if this PR updates REST search UI immediately
- Modify: `frontend/src/components/CitedMarkdown.tsx` only if accepting structured citations is low-risk

### Step 9.1: Add TypeScript type

```ts
export interface SourceRef {
  doc_id: string
  doc_title: string
  chunk_id?: string | null
  pages?: string | null
  section?: string | null
  source_kind: 'document' | 'email' | 'attachment' | 'unknown'
  source_label: string
  folder_label?: string | null
  relative_path?: string | null
  citation: string
}
```

### Step 9.2: Minimal UI use

If SearchPage is touched in this PR:

- Add `source?: SourceRef | null` and `citation?: string | null` to `SearchHit`.
- Render `hit.citation` where page/title citation text is currently assembled.
- Keep links based on existing `doc_id`/page fields.

If this makes the PR too broad, leave UI rendering to the Search Workbench plan and only add the type.

---

## Task 10: Full verification

Run targeted tests first, then broader suites:

```bash
uv run pytest tests/test_source_ref.py tests/test_source_ref_context.py -v
uv run pytest tests/api/test_search_source_ref.py tests/test_llm_citations.py -v
uv run pytest tests/test_mcp_tools.py tests/test_mcp_lookup_tools.py tests/test_mcp_metadata_filter.py -k "source_ref or source_path or search or read_passages or find_all or documents_by_date or verify_identifier" -v
uv run pytest tests/cli/test_output.py tests/cli/test_commands_search.py -v
```

If frontend files changed:

```bash
cd frontend && npm run type-check
cd frontend && npm run lint
```

If the change touches many MCP response shapes, also run:

```bash
uv run pytest tests/test_mcp_tools.py tests/test_mcp_response_steering.py tests/test_llm_citations.py -v
```

---

## Backwards Compatibility Checklist

- Legacy top-level fields are still present.
- `llm/citations.py` still parses old tool result JSON.
- CLI text output still handles old payloads.
- Existing tests that assert old fields continue to pass.
- No existing endpoint requires clients to understand `source`.
- No absolute paths appear in MCP/CLI `source` payloads.
- `kb_get_document` still omits `source_path`.

---

## Suggested PR Boundaries

If this becomes too large, split into:

1. **PR 1:** Pure builder, context loader, tests.
2. **PR 2:** REST search/passages SourceRef.
3. **PR 3:** MCP search/read/find-all SourceRef plus citation extractor.
4. **PR 4:** MCP document-row tools and CLI help/output.
5. **PR 5:** Research citation normalization and minimal frontend type/rendering.

PR 1 and PR 2 should land before Search Workbench implementation.

---

## Fresh-Eyes Review Triggers

Require review before merge if any PR:

- changes MCP or CLI response contracts;
- changes API schemas;
- changes path/source disclosure;
- changes chat/research citation extraction;
- changes verifier inputs;
- touches more than three files.

This work almost certainly triggers review.
