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
- New `kb_corpus_overview` tool: document count, total chunks, language distribution, topic clusters
- Precomputed stats vs live query TBD

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
| 2 | Document Summaries | Not started |
| 3 | Context Expansion | Not started |
| 4 | Scoped/Filtered Search | Not started |
| 5 | Document Outline | Not started |
| 6 | Corpus Overview | Not started |
| 7 | Cross-Document Similarity | Not started |
| 8 | Entity Extraction | Not started |
| 9 | Auto-Inject RAG | Not started |
