# PR-F — Document metadata extractors + structured-filter search

**Status:** spec — awaiting user review before plan + implementation
**Author:** Claude (with Alex)
**Companion docs:** PR-A/B/C answer-eval specs (`2026-05-22`, `2026-05-23`); follow-on PR-D (tool descriptions), PR-E (verify_identifier + by_date), PR-G (harness audit) all queued behind this.

## Goal

Capture machine-readable metadata that HC already has access to but isn't storing today (Tika fields, YAML frontmatter, JSON sidecars), surface it on a new `Document.metadata` JSONB column, and expose it as a structured filter on `kb_search` so models can disambiguate boundary-doc cases by fact rather than by similarity. This is real production utility — every Tika-extracted file gets metadata for free; Obsidian/MkDocs vaults get their frontmatter; the synthetic test corpus and any power user with sidecar files gets full structured facts.

## Why this PR exists (cross-corpus failure signal)

The PR-A/B/C eval runs surfaced a reproducible failure mode on both Sonnet and gpt-4o: **boundary-doc retrieval ambiguity**. When the question's identifier isn't unique in the corpus (e.g., "the Pinnacle vendor contract," "the Senior Project Coordinator onboarding letter," "the Marbledock Elevate 2025 campaign"), HC's similarity ranker picks a neighbouring doc that shares the identifier text but reports the wrong fact. Both models trust the retrieval and answer from the wrong doc.

The fix at the eval-prompt layer is partial — Sonnet correctly recognises the ambiguity (*"There are multiple Senior Project Coordinator onboarding letters in the corpus, each signed by a different manager"*) but has no way to resolve it without a structured filter. Adding `kb_search(metadata_filter={vendor: "Pinnacle Tech Solutions, LLC", term_months: 24})` lets the model pin doc 0131 vs doc 0149 by fact, not by string similarity. That's the leverage PR-F unlocks.

## Non-goals

- **No LLM-based metadata extraction.** Tika + frontmatter + sidecars cover the high-leverage real-world sources. Adding an LLM extraction step to the ingest pipeline (per-doc cost, prompt design, schema decisions) is its own future PR with its own design pass.
- **No folder-path-as-metadata.** Watched-folder hierarchy is real organizational signal but the user-facing config story (regex template, opt-in, per-folder vs global) is its own design pass. Worth a fast follow-up, not this PR.
- **No filename-pattern parsing.** Same reasoning — interesting heuristic source but needs its own design.
- **No retroactive backfill UI.** A one-shot Python re-ingest script ships with this PR for existing dev/test corpora. A user-facing "re-extract metadata for all docs" admin button is out of scope.
- **No metadata-driven UI in the React app.** This PR is plumbing + MCP exposure. Frontend display of metadata (e.g., a "Properties" panel on the document detail page) is a future polish PR.
- **No change to the existing typed email columns.** `email_from_address`, `email_to_addresses`, `email_message_id`, etc. on the `documents` table stay as-is — they're already populated by the email-ingest path (PR #283) and have FKs/indexes that benefit from being typed columns. The new generic JSONB metadata column coexists; email-ingest also writes its fields into the JSONB column for filter consistency, but the typed columns remain the source of truth for email-specific queries.
- **No multi-tenancy on metadata.** Single-tenant appliance — no tenant_id partitioning, same as the rest of the schema.

## Background — what's in HC today

**Document model** (`src/harbor_clerk/models/document.py`, 73 lines, flat post-0017): scalar columns for title, status, mime_type, summary, source_path, plus a block of typed email columns (`email_from_address`, `email_to_addresses`, `email_message_id`, `email_thread_id`, etc.) populated by the email-ingest path. **No generic metadata column exists.**

**Extract stage** (`src/harbor_clerk/worker/stages/extract.py`, 474 lines): calls Tika's `/tika` endpoint for body text and `/rmeta/text` for exception detail. **Tika's metadata response is currently discarded.** Tika returns a dict per file with fields like `dc:creator`, `dc:title`, `dc:subject`, `Last-Modified`, `xmpTPg:NPages`, custom XMP properties, EXIF for images, RFC 822 headers for `.eml` files (`Message-From`, `Message-To`, `Message-Subject`, `dcterms:created`), Office document custom properties, etc.

**MCP `kb_search`** (`src/harbor_clerk/mcp_server.py` + `src/harbor_clerk/llm/tools.py`): currently takes `query`, `k`, optional `doc_ids` filter. **No structured metadata filter exists.**

**Markdown ingest** (`src/harbor_clerk/worker/markdown_extract.py`): currently passes markdown through as text, captures wikilinks (PR #384). **Frontmatter is currently part of the body text, not parsed out.** Reading `---` frontmatter out and storing as metadata would also have the nice side effect of removing frontmatter from chunked text (no more noise like `tags: [foo, bar]` in retrieval).

## Architecture

A small extractor framework: each extractor is a stateless function that takes the raw file bytes + the partial Document object and returns a dict of metadata fields. The extract stage runs them in a documented order and merges results into a single namespaced JSONB blob stored on `Document.metadata`. The merge keeps source provenance so a later debugger can tell whether `author: "X"` came from Tika, frontmatter, or a sidecar.

```
src/harbor_clerk/
├── alembic/versions/00XX_add_documents_metadata.py   (new migration)
├── models/document.py                                 (add metadata column)
├── ingest/
│   └── metadata_extractors/                           (NEW package)
│       ├── __init__.py                                (extractor registry + run_all)
│       ├── tika_metadata.py                           (calls /meta, whitelists fields)
│       ├── frontmatter.py                             (parses YAML frontmatter from markdown)
│       └── sidecar.py                                 (loads <stem>.json from source_path)
├── worker/stages/extract.py                           (call run_all, persist to Document.metadata)
├── worker/markdown_extract.py                         (strip frontmatter from body text)
├── mcp_server.py + llm/tools.py                       (add metadata_filter param to kb_search)
├── api/routes/search.py                               (thread filter through to query layer)
└── search/engine.py (or wherever the query lives)     (translate filter to JSONB WHERE clause)
```

## Metadata schema (namespacing + precedence)

Document.metadata is a JSONB column. Top-level structure:

```jsonc
{
  "tika":        {"author": "...", "subject": "...", "page_count": 3, ...},
  "frontmatter": {"tags": ["x", "y"], "status": "draft", "project": "alpha", ...},
  "sidecar":     {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24, ...},
  "_source_provenance": {"tika": "2026-05-24T...", "sidecar": "2026-05-24T..."}
}
```

**Why namespaced instead of flat:**
- Avoids collision when two sources have the same key (e.g., both Tika and frontmatter expose `author`)
- Lets the filter API be precise: `metadata_filter={"sidecar.vendor": "Pinnacle"}` vs `metadata_filter={"tika.author": "Jane Doe"}`
- Debugging: when a filter doesn't match, you can see immediately which source contributed (or didn't) which field
- Makes Tika's noisy 50–200-field output containable — it lives under one key, doesn't pollute the top namespace

**`_source_provenance`** records the timestamp each extractor wrote its slice — useful for "re-ingested vs first-pass" debugging and for a future "re-run only the X extractor" optimization.

**Precedence on conflict:** Within a namespace, last-write-wins. Across namespaces, all coexist (no merging across namespaces — they're independent slices). For the rare case where two extractors of the same type are added (e.g., Tika + a Tika-equivalent), namespace them differently (`tika_v2`).

## Extractor framework

```python
# src/harbor_clerk/ingest/metadata_extractors/__init__.py
from typing import Protocol

class MetadataExtractor(Protocol):
    name: str  # namespace key — "tika", "frontmatter", "sidecar"

    def extract(self, *, doc: Document, raw_bytes: bytes, source_path: str | None) -> dict | None:
        """Return a dict of metadata fields, or None if this extractor doesn't apply
        (e.g., frontmatter extractor on a PDF). Returning None skips the namespace
        entirely — namespaced output stays clean."""
        ...

EXTRACTORS: tuple[MetadataExtractor, ...] = (
    TikaMetadataExtractor(),
    FrontmatterExtractor(),
    SidecarExtractor(),
)

def run_all(*, doc, raw_bytes, source_path) -> dict:
    """Run all extractors in order; merge results into a namespaced blob."""
    out: dict = {}
    provenance: dict = {}
    for ext in EXTRACTORS:
        try:
            result = ext.extract(doc=doc, raw_bytes=raw_bytes, source_path=source_path)
            if result:
                out[ext.name] = result
                provenance[ext.name] = utc_now().isoformat()
        except Exception as exc:
            log.warning("metadata extractor %s failed on doc %s: %s", ext.name, doc.doc_id, exc)
    if out:
        out["_source_provenance"] = provenance
    return out
```

Each extractor is a separate module, tested independently. The framework's job is just to run them and merge. **No extractor is required to succeed** — a failure logs a warning and the doc gets the other extractors' output. This keeps ingestion resilient to one-off Tika hiccups or malformed frontmatter.

### TikaMetadataExtractor

Calls Tika's `/meta` endpoint (vs the existing `/tika` for body text). Tika returns a JSON dict per file with potentially 50–200 fields, most of which are noise (X-TIKA-Parsed-By, X-TIKA-Content-Length, etc.). **Whitelist** to a curated set:

```python
TIKA_FIELD_ALIASES = {
    # Generic Dublin Core
    "dc:creator": "author",
    "dc:title": "title",
    "dc:subject": "subject",
    "dc:description": "description",
    "dc:language": "language",
    "dcterms:created": "created_at",
    "dcterms:modified": "modified_at",
    "meta:keyword": "keywords",
    # Pagination / structure
    "xmpTPg:NPages": "page_count",
    "Page-Count": "page_count",
    # MIME / encoding
    "Content-Type": "content_type",
    "Content-Encoding": "encoding",
    # Email-specific (RFC 822 headers from Tika's email parser)
    "Message-From": "email_from",
    "Message-To": "email_to",
    "Message-Cc": "email_cc",
    "Message-Subject": "email_subject",
    "dcterms:created": "email_date",  # in email context, this is Send-Date
}
```

Aliases normalize wildly-varying Tika field names to readable filter keys. Unknown Tika fields are dropped. **No raw passthrough** — keeps the metadata blob bounded and the filter surface clean.

### FrontmatterExtractor

For markdown files (`.md`, `.markdown`), parse YAML frontmatter delimited by `---`. Uses the `python-frontmatter` library (a ~10 kB wrapper around PyYAML; well-maintained, widely-used by Jekyll/Hugo/MkDocs tooling). Added to `pyproject.toml` as a runtime dep. Returns the parsed frontmatter dict as-is. Side effect: signal to `markdown_extract.py` that frontmatter has been parsed so it can be stripped from the body text before chunking.

For non-markdown formats: returns `None`.

### SidecarExtractor

Looks for `<stem>.json` next to `source_path`. If found, loads the JSON as the namespace contents. For the synthetic corpus this is the existing pattern (sidecar generated alongside the doc). For real-world power users, this gives them an opt-in escape hatch for metadata HC doesn't otherwise capture.

For docs without a `source_path` (legacy uploaded docs, the few rows left in `uploads`): returns `None`.

## kb_search filter API

New optional parameter on `kb_search`:

```python
kb_search(
    query: str,
    k: int = 10,
    metadata_filter: dict[str, Any] | None = None,
    # ... existing params ...
) -> dict
```

`metadata_filter` is a flat dict of `"<namespace>.<key>": value` pairs. The query layer translates this to a JSONB containment WHERE clause:

```sql
WHERE metadata @> '{"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC"}}'::jsonb
  AND metadata @> '{"sidecar": {"term_months": 24}}'::jsonb
```

Multiple filter keys combine with `AND`. The `@>` containment operator uses the GIN index efficiently. Models can stack filters:

```python
kb_search(
    query="governing law",
    metadata_filter={
        "sidecar.vendor": "Pinnacle Tech Solutions, LLC",
        "sidecar.term_months": 24,
    },
    k=3,
)
```

This pins the right Pinnacle contract among many. When filters match nothing, the query degrades to an empty result set (model can re-query without filters). When filters are absent, behavior is unchanged from today.

**Tool description for the new param** (lives in PR-D's full pass, but the param ships with PR-F): "Use `metadata_filter` to disambiguate when multiple candidate docs share text but differ on a structured field. Example: `metadata_filter={'sidecar.vendor': 'Acme', 'sidecar.term_months': 24}`. Inspect a document's available fields via `kb_get_document` first if you don't know what keys are available."

## Re-ingest strategy

Existing docs in dev/test corpora need a one-shot metadata-extraction pass. Two paths:

1. **Per-corpus reprocess script** (`scripts/reextract_metadata.py`): iterates Documents in the test workdir, fetches raw bytes from storage (or rereads from `source_path` for watched-folder docs), runs the extractor framework, writes results. Idempotent — running twice produces the same metadata.

2. **Just re-ingest** for small corpora: delete the synthetic ingest, re-run the corpus acquire, fresh extract stage picks up new metadata for free.

For production HC users: existing docs get metadata on the next time they're re-extracted (rare event — only after delete+re-add or admin "Reprocess" button). Most existing docs stay metadata-free until then. **Acceptable** — the new tools degrade gracefully without metadata; new ingestions get the benefit immediately.

## Migration

Single alembic migration:

```python
def upgrade():
    op.add_column("documents",
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default="{}"))
    op.create_index("ix_documents_metadata_gin", "documents", ["metadata"],
                    postgresql_using="gin")

def downgrade():
    op.drop_index("ix_documents_metadata_gin", table_name="documents")
    op.drop_column("documents", "metadata")
```

Default empty object (not NULL) so the `@>` containment query never has to special-case NULL. GIN index supports the containment operator efficiently.

Naming note: `metadata` is a reserved column name in some ORMs (SQLAlchemy uses `__tablename__.metadata` as the MetaData object reference). The actual Python attribute on the Document model will need to be named `doc_metadata` or similar to avoid shadowing — call out during implementation. The PostgreSQL column name stays `metadata`.

## Validation plan

1. **All existing tests pass + new extractor tests** in `tests/ingest/metadata_extractors/`:
   - Per-extractor unit tests (mock Tika response, sample markdown with frontmatter, sample JSON sidecar)
   - Framework integration test (run_all with a mix of extractors that succeed/fail/skip)
   - kb_search filter query test (against a small test DB with known metadata)
2. **Ruff + frontend type-check clean**.
3. **Live validation against the synthetic corpus**:
   - Re-ingest synthetic so every doc gets sidecar metadata populated.
   - Verify `kb_search(metadata_filter={"sidecar.vendor": "Pinnacle Tech Solutions, LLC"})` returns exactly the Pinnacle contracts.
   - Re-run answer-eval on synthetic with both providers — expect to see boundary-doc lookup correctness improve once PR-D wires the filter into tool descriptions. (PR-F alone, without PR-D's description telling models to USE the filter, won't change scores. Validation here is "filter works mechanically"; the score-improvement validation is PR-D's.)
4. **Tika field sanity check on a real-world doc set**:
   - Re-ingest the existing watched-folder corpus on the user's mac
   - Spot-check 10 docs of varying types (PDF, DOCX, .eml, .md): the extracted metadata should be sensible (author, dates, subject populated where Tika has them)
   - Document expected/surprising fields in the PR description for future reference
5. **Performance sanity** — Tika `/meta` call adds latency to the extract stage. Measure on a 100-doc batch; expect <10% overhead since extract is already Tika-bound. If it's worse, evaluate parallel calls or single-pass extraction (Tika supports returning body + meta together via `/rmeta/text`).

## Risks

- **Tika metadata is wildly inconsistent across formats.** PDF metadata is sparse; DOCX is rich; `.eml` has structured headers; images have EXIF (or nothing). The whitelist + alias map handles this, but expect the field surface to be uneven across a real corpus. Documented expectation, not a defect.
- **Frontmatter parsing failures** on malformed YAML. The extractor catches + logs + returns None — but a doc with bad frontmatter gets no metadata namespace at all (not even the partial parse). Acceptable.
- **Sidecar trust boundary.** Loading arbitrary user-supplied JSON next to a doc means user-controlled metadata. Filter values go into a SQL containment query, but SQLAlchemy's JSONB binding parameterizes properly — no SQL injection. Worth noting for review.
- **Metadata can override per-doc reality.** If a user has a sidecar saying `vendor: "Acme"` but the actual contract is with Pinnacle, the filter answers questions about the sidecar's claim, not the doc's text. This is "garbage in, garbage out" and acceptable — users with sidecars are power users who know what they're declaring.
- **JSONB column doesn't suit huge metadata blobs.** Tika can return very large metadata for some files (EXIF with thumbnails embedded as base64, etc.). The whitelist cap protects against this; worth noting in the field list documentation.

## Open questions for spec review

- **Strip frontmatter from body text or leave it in?** Leaning strip — the metadata fields cover it and leaving them in adds noise to chunking + retrieval. But some users might write meaningful prose IN their frontmatter (e.g., a long `summary:` field). Decision: strip, document the behavior, revisit if users complain.
- **`kb_get_document` should return the metadata** so models can inspect what filter keys are available. This is a small change in scope; include in PR-F or queue for PR-D? Leaning include here — keeps the surface coherent.
- **`metadata_filter` value matching: exact only, or operators (>=, contains, in)?** Leaning exact-only for v1 — covers the boundary-doc disambiguation case. Operator support is a clean follow-up.
- **List-valued field matching: `metadata_filter={"frontmatter.tags": "alpha"}` should match a frontmatter dict `{tags: ["alpha", "beta"]}`.** JSONB `?` operator handles this, but the query layer needs to know when to use `?` vs `@>`. Leaning: any scalar filter value matches via `@>` against either scalar metadata values OR list-valued metadata values that contain the scalar. Document the behavior.

## Out of scope / follow-ups

- **PR-D (tool descriptions + kb_search response enhancements)** — sits on top of PR-F to surface the new filter capability in tool descriptions; ships immediately after.
- **PR-E (kb_verify_identifier + kb_documents_by_date)** — `kb_verify_identifier` can now do exact metadata lookups; PR-E benefits architecturally from PR-F landing first.
- **PR-G (harness audit + cross-judge sensitivity)** — pure harness work, can land in parallel with D/E if desired.
- **LLM-based metadata extraction** — separate PR with its own design. Tika + frontmatter + sidecar covers the no-LLM-cost layer.
- **Folder-path-as-metadata** — separate PR; user-facing config story (regex template, opt-in) deserves its own design pass.
- **Filename-pattern parsing** — separate PR; similar config story.
- **Frontend display of metadata** — a "Properties" panel on the document detail page. UI polish, separate PR.
- **Admin "Reprocess Metadata" button** — bulk re-extraction trigger for production users who want to populate metadata on existing docs without delete+re-add. Future UX.
- **Folder-level metadata config** — `.harbor-clerk.yaml` in a watched folder declaring "every doc here has type=invoice, client=Acme". Deferred to a future "folder metadata config" PR after we see real-world usage patterns.
