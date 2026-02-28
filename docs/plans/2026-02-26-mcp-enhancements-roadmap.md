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
| 9 | Auto-Inject RAG | Not started |

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

### Entity Co-occurrence Graph

Once Feature 8 (Entity Extraction) is in place, a natural extension is a `kb_entity_graph` tool that finds entities mentioned near a given entity (within N chunks). This enables relationship discovery — e.g., "which organizations are mentioned alongside John Smith?" Deferred because the core entity search + overview tools cover the primary use case; co-occurrence is a power-user feature that can be layered on top of the same `entities` table without schema changes.
