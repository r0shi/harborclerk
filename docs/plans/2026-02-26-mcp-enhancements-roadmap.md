# MCP Server Enhancements Roadmap

**Goal**: Transform Harbor Clerk's MCP server from a basic search tool into a comprehensive knowledge navigation system that gives LLM agents real leverage over the corpus.

**Context**: Current MCP tools offer 7 operations (kb_search, kb_read_passages, kb_get_document, kb_list_recent, kb_ingest_status, kb_reprocess, kb_system_health). Search returns top-K chunks (~1K chars each) via hybrid FTS+vector. The agent has no bird's-eye view, no way to navigate document structure, and limited ability to iteratively explore.

---

## Feature List (priority order)

### 1. Adjustable K + Pagination on Search
- Let the LLM request variable result counts
- Add cursor-based pagination for walking large result sets
- Currently hardcoded `k` default 10, max 100; no pagination

### 2. Document Summaries at Ingest
- Generate per-document summaries during finalize stage
- Store in new column on `document_versions`
- Expose via MCP tools (kb_get_document, kb_list_recent, new kb_corpus_overview)
- Summarization strategy TBD (extractive vs LLM vs hybrid)

### 3. Context Expansion (Surrounding Chunks)
- New `kb_expand_context` tool: given chunk ID, return N chunks before/after
- Builds on existing chunk_num ordering within a version
- kb_read_passages already has `include_context` but only ±1 chunk

### 4. Scoped/Filtered Search
- Filter by document ID(s), date range, language, mime type
- "Search within document X" use case
- Faceted results: group hits by document with per-doc relevance

### 5. Document Outline/Structure
- New `kb_document_outline` tool: headings, sections, page count, chunk count
- Extract structural metadata from Tika XHTML during extract stage
- Store in document_pages or new table

### 6. Corpus Overview
- Enhance existing `kb_corpus_overview` tool with aggregate stats: language distribution, mime type breakdown, total pages, date range
- Live queries (no precomputation needed at single-tenant scale)
- REST endpoint mirror

### 7. Cross-Document Similarity
- New `kb_find_related` tool: given doc/chunk ID, find nearest neighbors
- Leverage existing embeddings (average chunk embeddings per document)
- Cheap to implement, high discovery value

### 8. Entity Extraction + Index
- Extract named entities (people, places, orgs) during chunk stage
- Store in new table, expose via MCP tool
- Highest effort, medium standalone value

### 9. Auto-Inject RAG Context in Chat
- When user asks a question in chat, auto-search and inject relevant chunks
- Hybrid: RAG for fast path, MCP tools for agent deep-dive
- Chat-specific enhancement, not MCP tool per se

---

## Status

Features 1–9 shipped in v0.4.0. Features 10–12 are the next batch.

| # | Feature | Status |
|---|---------|--------|
| 1 | Adjustable K + Pagination | **Done** |
| 2 | Document Summaries | **Done** |
| 3 | Context Expansion | **Done** |
| 4 | Scoped/Filtered Search | **Done** |
| 5 | Document Outline | **Done** |
| 6 | Corpus Overview | **Done** |
| 7 | Cross-Document Similarity | **Done** |
| 8 | Entity Extraction | **Done** |
| 9 | Auto-Inject RAG | **Done** |
| 10 | Entity Co-occurrence | **Done** |
| 11 | Full-Document Retrieval | **Done** |
| 12 | Batch Search | Planned |

---

## Feature List (next batch)

### 10. Entity Co-occurrence Queries

New `kb_entity_cooccurrence` tool: given an entity (text + optional type), find other entities that co-occur with it. Configurable scope — same chunk (tight proximity, strong signal) or same document (broader net).

- **Impact: High.** "Which organizations are mentioned alongside John Smith?" is a daily question for law firms, researchers, policy analysts. This is the relationship-discovery layer on top of Feature 8.
- **Effort: Low-Medium.** Query-layer feature only — self-join on existing `entities` table, no new ingestion stage, no schema migration, no CPU cost.
- **Parameters:** `entity_text` (required), `entity_type` (optional filter on the target entity), `scope` (chunk | document, default chunk), `cooccur_type` (optional filter on co-occurring entity types), `doc_id` (optional scoping), `limit`, `offset`
- **Returns:** Co-occurring entities with mention counts, scoped appropriately. Each result includes entity_text, entity_type, cooccurrence_count, and sample doc_ids/chunk_ids for citation.
- **YAGNI risk: Low.** If you have entity extraction, co-occurrence is the natural next question.

### 11. Full-Document Retrieval

New `kb_read_document` tool: retrieve the full extracted text of a document, paginated by page range. For short documents (memos, policies, one-page letters), an LLM needs to read the whole thing without chaining chunk-by-chunk reads.

- **Impact: Medium.** "Summarize this document" is a common request. Currently requires chaining `kb_expand_context` or guessing chunk IDs.
- **Effort: Low.** Query all chunks for a version ordered by chunk_num, paginate by page range. All data already exists.
- **Parameters:** `doc_id` (required), `page_start` (optional, default 1), `page_end` (optional, default last page)
- **Returns:** doc_id, version_id, title, page_count, total_chars, pages requested, and concatenated text (with page break markers). Caps output to prevent returning a 500-page PDF in one call.
- **YAGNI risk: Low-Medium.** Short documents are common in all target use cases.

### 12. Batch Search

New `kb_batch_search` tool: run multiple search queries in a single call, returning results grouped by query. Saves round-trips during comparative analysis ("do any of these case files mention X, Y, or Z?").

- **Impact: Medium.** Reduces latency for cross-cutting analysis. Each round-trip is time the user waits.
- **Effort: Low.** Loop over existing `hybrid_search`, return grouped results.
- **Parameters:** `queries` (list of strings, max 5), plus shared filter params (doc_id, doc_ids, after, before, language, mime_type), `k` per query, `detail`
- **Returns:** Array of result sets, one per query, same shape as `kb_search` response.
- **YAGNI risk: Medium.** LLMs can call `kb_search` sequentially, but batching is a genuine convenience for multi-query patterns.

---

## Future Improvements

### Topic Clustering for Multi-Project Navigation

When Harbor Clerk gains multiple datasets/projects within a single tenant (project switching, not multi-tenancy), corpus overview becomes the **project-switching UI** — the agent needs to quickly understand "what's in Project A vs Project B."

The natural zoom hierarchy would be:
- **Corpus overview** → list projects (name, doc count, summary)
- **Project overview** → list documents (title, summary, mime type)
- **Document outline** → headings, pages, chunks

Per-project aggregate stats (language distribution, mime type breakdown) already serve much of this need. True embedding-based topic clustering (k-means on averaged doc embeddings, label extraction) would only add value when a single project has 100+ diverse documents where stats alone don't convey the content mix.

**Decision:** Defer topic clustering. The aggregate stats from Feature 6 are exactly what would be scoped per-project later. Topic clustering is an optional enhancement for large, diverse corpora — not an architectural prerequisite.

### Apple Intelligence as Summarization Fallback

Use macOS 26's Foundation Models framework as a middle-tier summarization fallback — better than extractive, available without downloading a model. The fallback chain becomes: (1) user-selected model via llama-server, (2) Apple Intelligence via Foundation Models, (3) extractive heuristic.

Since Foundation Models is Swift-only, the approach is a standalone Swift CLI tool (`apple-intelligence-server`) exposing `POST /v1/chat/completions` (OpenAI-compatible subset) on port 8103. ServiceManager spawns it like any other subprocess. Python's `generate_summary()` tries `llama_server_url` first, then `apple_intelligence_url`, then extractive. Only started when no user-selected LLM model is active.

Full plan: `docs/plans/2026-02-26-apple-intelligence-summarization-fallback.md`

Open questions: Foundation Models availability detection, context window limits, first-call model load latency, build gating for macOS 26+ only. Not in scope: using Apple Intelligence for chat, replacing llama-server, or any cloud API calls.

### Adaptive Tool Schema Complexity

Chat tools currently use simplified parameter schemas (e.g., `search_documents` exposes 3 of kb_search's 12 params) because small local models (4-8B) struggle with many parameters. As average local model capability increases, revisit this.

Possible approaches:
- **Tiered schemas**: "simple" and "full" schema sets, selected by model metadata (param count, context window)
- **Progressive disclosure**: Model requests richer schemas mid-conversation when it needs advanced filtering
- **Model self-assessment**: The model recognizes it needs a parameter it doesn't have and asks for an upgraded tool definition

No good automatic heuristic yet — context window size correlates loosely with tool-handling ability but isn't a reliable proxy. For now, the simplified schemas work and the full MCP tools remain available to external agents (Claude, etc.) that can handle the complexity.

### DATE Entity Filtering Improvements

Improve `kb_entity_search` for date-type queries. Currently DATE entities are stored as raw text ("March 2020", "2019-01-15"), making them hard to filter or sort meaningfully. Low-effort improvements without a new pipeline stage:
- Sort DATE entities chronologically when `entity_type=DATE` (best-effort parsing of common date formats)
- Add a `sort` param to `kb_entity_search` (e.g., `sort=chronological` for dates, `sort=frequency` for others)
- No date normalization or temporal range extraction — that's a separate, high-effort feature (see below)

**Decision:** Low-effort query improvement. No new ingestion, no CPU cost. Useful but not urgent — DATE entities are already queryable via substring search.

### Temporal Awareness (Date Range Extraction)

Full chunk-level temporal awareness: extract and normalize date references during ingestion, store as queryable date ranges, enable "find chunks about events in 2020-2021" queries. Would require NLP date extraction (beyond spaCy's NER), normalization to ISO ranges, disambiguation (document date vs event date), new schema, new query interface.

**Decision:** Deferred. High effort, high ambiguity (does "March 2020" mean authored then or describes an event then?), highly corpus-dependent. The existing DATE entity extraction from Feature 8 captures the raw strings; this would be a major extension. Revisit if user feedback shows strong demand.

### PostgreSQL 17 Upgrade (target: v0.5.0)

Upgrade from PostgreSQL 16 to 17 for next minor release. PG 17 is stable since September 2024 (now at 17.9). Notable features: new VACUUM memory management, `JSON_TABLE()`, streaming sequential I/O, improved write throughput. PG 16 EOL is November 2028, so this is opportunistic, not urgent.

**Approach:**
- Bump `PG_VERSION` in `build-postgres.sh` to `17.x`, Docker image to `pgvector/pgvector:pg17`
- **Rebase Alembic migrations**: Collapse all 14+ migration files into a single initial migration reflecting the final schema. This eliminates accumulated cruft and makes the schema easier to reason about going forward.
- **Require full re-import** for the 0.X.0 upgrade: document data is re-ingestible, so users delete their data directory and re-upload. No `pg_upgrade` needed, no backward-compatible migration path.
- Update `initdb` args in `PostgresService.swift` if any PG 17-specific flags are needed (unlikely).
- Update Docker base images (`docker/app.Dockerfile` builder stage if needed).

This is a clean breaking change appropriate for a pre-1.0 release.
