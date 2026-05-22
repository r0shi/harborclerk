# Harbor Clerk Retrieval/MCP Upgrade — Master Staging Plan

> **For agentic workers:** This is a STAGING plan, not an implementation plan. It maps out 6 independent phases. Each phase needs its own detailed implementation plan written via `superpowers:brainstorming` → `superpowers:writing-plans` before execution. Do NOT attempt to implement any phase directly from this document.

**Goal:** Take Harbor Clerk's retrieval pipeline from `multilingual-e5-small` + hybrid FTS/cosine to a SOTA stack optimized for frontier-LLM MCP consumption, with a queryable knowledge graph, layered preprocessing, and visual exploration — all gated by per-phase retrieval-eval against a frozen eval fixture.

**Architecture:** Six independent phases, each shipping behind retrieval-eval gates. Phase 0 is the foundation (embedding/reranker swap + schema break). Phases 1-2 are "free wins" (no new infrastructure). Phases 3-4 add the graph and propositions layers. Phase 5 is UI. Each phase is isolated on its own branch, merges after passing retrieval-eval against the prior phase's label, and bumps the schema sentinel where applicable.

**Tech Stack:** PostgreSQL 18 + pgvector + pg_trgm, FastAPI + SQLAlchemy 2.0 async, IBM Granite Embedding 311M Multilingual R2, BAAI/bge-reranker-v2-m3, GLiNER + GLiNER-Relex, Microsoft Presidio, BERTopic (existing), spaCy (existing), Vite + React + TypeScript + Tailwind 4 + d3-force (already in deps), Apache Tika (existing).

---

## How To Use This Document

1. **Read top-to-bottom** for the staging story and inter-phase dependencies.
2. **Before starting phase N**: invoke `/brainstorm` to nail down the open questions for that phase (see "Open Questions Per Phase" at the bottom), then `/write-plan` to produce a bite-sized detailed plan, saved alongside this one as `2026-MM-DD-retrieval-upgrade-phase-N-<topic>.md`.
3. **Each phase is its own branch**, its own PR, its own retrieval-eval label. Never bundle phases.
4. **Each phase passes through the gate** before the next phase starts. If the gate fails, do not advance — fix or revise.
5. **Memory hygiene**: every phase PR with deferred work → entry in [`pr_followups.md`](https://github.com/alex/mcp-gateway/blob/main/memory/pr_followups.md). Every cross-phase decision worth remembering → its own memory file.

---

## Phase Map

| Phase | Branch prefix | Goal | Schema change? | Expected recall@10 vs e5-small baseline | Effort |
|---|---|---|---|---|---|
| 0 | `feat/retrieval-phase-0-granite` | Granite-R2 + bge-reranker-v2-m3 + schema sentinel + fail-stop | YES — `vector(384)` → `vector(768)`, breaking, non-reversible | +0.10 to +0.18 | M (1-2 weeks) |
| 1 | `feat/retrieval-phase-1-late-chunking` | Late chunking + multi-granularity (chunk/section/doc) + heading-chain in passages | YES — new `chunk_embeddings` index strategy, new `section_embeddings` + `document_embeddings` tables | +0.04 to +0.10 | M (1-2 weeks) |
| 2 | `feat/retrieval-phase-2-metadata` | Doc-type classifier + temporal extraction + PII tagging + table extraction | YES — new columns on `documents` and `chunks`, new `document_tables` table | +0.03 to +0.05 (precision on filtered queries: +0.20-0.40) | M (1-2 weeks) |
| 3 | `feat/retrieval-phase-3-graph` | Entity canonicalization + relation extraction + cross-doc linking + 5 new MCP tools | YES — `entity_canonical`, `entity_relations`, `document_references` tables | +0.03 to +0.08 (multi-hop queries: much larger) | L (2-3 weeks) |
| 4 | `feat/retrieval-phase-4-propositions` | Propositional indexing + HippoRAG-style multi-hop + `kb_find_fact` + `kb_multihop_search` | YES — `propositions` table | +0.05 to +0.10 (fact-extraction queries: +0.15-0.25) | L (2-3 weeks) |
| 5 | `feat/retrieval-phase-5-ui` | Force-directed entity graph, UMAP topic projection, doc-type composition, retrieval inspector | NO | N/A (UX delta, not retrieval delta) | M (1-2 weeks) |

**Total**: ~10-13 weeks of focused work. Realistically 4-6 months with normal interruptions and the existing roadmap. Phases 0-2 can be fast (well-defined). Phases 3-4 will trip on real-corpus surprises. Phase 5 is fun.

**Expected cumulative delta after all 6 phases:** recall@10 of 0.85-0.95 on the sweep questions (e5-small baseline is presumably 0.50-0.70 depending on corpus). The biggest single lever is Phase 0; the highest-novelty value-add for frontier LLMs is Phase 3-4.

---

## Cross-Cutting Decisions

These apply across every phase and must be respected without re-litigation.

### Branch + PR Discipline
- One branch per phase, named `feat/retrieval-phase-N-<topic>`.
- One PR per phase. Don't bundle.
- Open the PR draft early; iterate; mark ready when retrieval-eval gate passes.
- **Final pre-merge step on every phase PR**: fresh-eyes `feature-dev:code-reviewer` subagent against branch tip with minimal prompt (per [feedback_capture_pr_followups.md](https://github.com/alex/mcp-gateway/blob/main/memory/feedback_capture_pr_followups.md) Directive 2). Address findings ≥80 confidence before merging.
- **No `gh pr merge --admin`** without explicit per-session permission.

### Schema Sentinel + Fail-Stop
Phase 0 introduces a `schema_metadata` table:
```sql
CREATE TABLE schema_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO schema_metadata (key, value) VALUES
  ('embed_model', 'granite-embedding-311m-multilingual-r2'),
  ('embed_dim', '768'),
  ('reranker', 'bge-reranker-v2-m3'),
  ('upgrade_phase', '0');
```

On API + worker boot (before opening the pool), query the sentinel rows; if `(embed_model, embed_dim)` doesn't match `settings.embed_model` / `settings.embed_dim`, log a loud panic and `sys.exit(2)`. Same check at the top of the `embed` worker stage. Belt-and-suspenders: bump `alembic_version` head; refuse to start if it's below the phase's minimum.

Each subsequent phase that adds schema bumps `upgrade_phase` (e.g. Phase 1 sets it to `'1'`). The fail-stop accepts only the current and prior phase numbers, so a Phase-1 binary refuses to open a Phase-0 DB unless explicitly migrated. This protects against deploying a new binary against a stale DB.

### Retrieval-Eval Gates
Use the `--mode retrieval-eval` harness against the **frozen `eval-fixture-v1` corpus + baselines** — see `docs/superpowers/specs/2026-05-21-retrieval-eval-corpus-design.md` and its setup plan `docs/superpowers/plans/2026-05-21-eval-fixture-setup.md`. The fixture (unified CUAD + Enron-500 + synthetic, ingested once, never re-ingested) and its title-bearing baselines must exist before any phase gate can run. Per-phase gate:
1. Before merging phase N, run: `sweep --mode retrieval-eval --run-id eval-fixture-v1 --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora" --api-base http://localhost:8100 --label phase-N --prior-label phase-(N-1)`
2. Read `results/eval-fixture-v1/retrieval-eval/phase-N/vs_phase-(N-1).json` for per-question deltas + `summary.json` for the overall recall@10.
3. Acceptable outcomes:
   - **Phase 0**: recall@10 improvement ≥+0.08 overall; no per-corpus regression more than -0.05 (CUAD/Enron/synthetic).
   - **Phase 1**: recall@10 improvement ≥+0.03 overall; no regression worse than -0.03 per corpus.
   - **Phase 2**: recall@10 may stay flat or improve; precision on filtered queries should improve (separate eval — see Phase 2 detail).
   - **Phase 3-4**: recall@10 improvement ≥+0.03; multi-hop subset shows much larger gains (separate eval slice).
   - **Phase 5**: not retrieval-gated.
4. If a phase fails its gate, do NOT advance. Either revise the phase or roll back parts and re-eval.

### Re-Embed Cost
Phases 0, 1, 4 require re-embedding all chunks (or building new embedding tables). For a 10K-doc corpus with ~500K chunks at ~50ms/chunk (Granite-R2 on M-series silicon), that's ~7 hours single-threaded. Plan re-embed as an explicit task with progress monitoring. The `embed` stage already has heartbeats; reuse the pipeline.

### Eval Fixture (supersedes the original "use the sweep baselines" assumption)
The original plan assumed the `2026-05-05-prod` sweep baselines could gate retrieval directly. **They can't** — the sweep re-ingests between phases (doc UUIDs churn) and HC holds one corpus at a time, so UUID-matched baselines always score 0 against a re-ingested corpus. The 2026-05-21 embedding-v2 gate run hit exactly this (all-zero) and the failure is written up in `docs/superpowers/specs/2026-05-21-retrieval-eval-corpus-design.md`.

The gate instead uses a dedicated **`eval-fixture-v1`**: a unified CUAD + Enron-500 + synthetic corpus ingested **once** into the eval box's `lka` and never re-ingested, plus baselines captured once (with `cited_doc_titles` — matching is now title-based, commit `58b5c04`). Build it via `docs/superpowers/plans/2026-05-21-eval-fixture-setup.md`.

**Guard rail:** the model-comparison sweep (the 8-local-model matrix, which re-ingests corpora per phase) must NOT run on the eval box during the upgrade — it would destroy the frozen fixture and force a full re-baseline. Run that sweep on a different box or a different time.

### Memory & Followups
- Every phase that defers out-of-scope work → one entry in `pr_followups.md` AND one entry in the PR description.
- Every cross-phase decision worth remembering (e.g. "we evaluated GLiNER-Relex vs REBEL, chose GLiNER-Relex because X") → one memory file under `~/.claude/projects/-Users-alex-mcp-gateway/memory/`.

---

## Phase 0: Granite-R2 + Reranker + Schema Hard Fail-Stop

### Goal
Replace `multilingual-e5-small` (118M / 384-dim) with `ibm-granite/granite-embedding-311m-multilingual-r2` (311M / 768-dim). Add `BAAI/bge-reranker-v2-m3` as a cross-encoder precision stage on top of hybrid retrieval. Introduce the schema sentinel that hard-stops on mismatch.

### File Structure
**Create:**
- `embedder/src/embedder/reranker.py` — Reranker server: loads bge-reranker-v2-m3, exposes `POST /rerank` taking `{query, passages: list[str], top_k}` returning `[{index, score}]` sorted desc.
- `alembic/versions/0023_schema_sentinel_and_granite_embed_dim.py` — Migration: create `schema_metadata` table, populate sentinel rows, ALTER `chunks.embedding` from `vector(384)` to `vector(768)`, drop + rebuild HNSW index, set `upgrade_phase=0`. Non-reversible (`downgrade()` raises `NotImplementedError`).
- `src/harbor_clerk/models/schema_metadata.py` — SQLAlchemy model.
- `src/harbor_clerk/db_health.py` — `verify_schema_sentinel()` called at API + worker boot.
- `src/harbor_clerk/search_rerank.py` — `rerank_hits()` async function: HTTP call to reranker, returns re-ordered hits.
- `tests/test_db_health.py` — Sentinel mismatch panic-exits the process.
- `tests/test_search_rerank.py` — Reranker re-orders hits, handles HTTP failures, respects top_k.
- `tests/test_embedder_granite.py` — Smoke: model loads, returns 768-dim embeddings, handles batch=64.

**Modify:**
- `embedder/src/embedder/app.py` — Default `EMBED_MODEL` to `ibm-granite/granite-embedding-311m-multilingual-r2`; remove the query/passage prefix logic (Granite uses CLS pooling, no prefix needed); keep the `task` param accepted but ignored for backward compat.
- `embedder/pyproject.toml` — Pin sentence-transformers version known to load Granite-R2.
- `src/harbor_clerk/config.py` — Add `embed_model`, `embed_dim`, `reranker_url`, `rerank_enabled`, `rerank_pool_size` settings.
- `src/harbor_clerk/search.py` — After hybrid merge, if `rerank_enabled` and pool ≥ K, call `rerank_hits(query, top_pool, k)` before truncating to top-K.
- `src/harbor_clerk/api/main.py` — Call `verify_schema_sentinel()` in lifespan startup.
- `src/harbor_clerk/worker/__init__.py` (or wherever worker boots) — Same call.
- `src/harbor_clerk/worker/stages/embed.py` — Increase per-stage timeout from 1800s to 3600s; Granite-R2 is ~5× slower per chunk.
- `docker/embedder.Dockerfile` — Bake Granite-R2 weights at image build OR document model download on first run.
- `docker-compose.yml` — Add `reranker` service (or co-host in embedder); env vars for HC to talk to it.
- `macos/scripts/install-services.sh` — Add reranker subprocess to macOS launch sequence; HarborClerkServer menubar must start/stop/healthcheck it.
- `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — New service definition for reranker.
- `frontend/src/pages/SystemStatusPage.tsx` — Show reranker health alongside embedder.

### Schema Migration
Single migration `0023_schema_sentinel_and_granite_embed_dim.py`:
1. Create `schema_metadata` table.
2. Insert sentinel rows: `embed_model`, `embed_dim`, `reranker`, `upgrade_phase`.
3. `ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768)` — Postgres rejects this when column has data, so: `DROP COLUMN embedding`, then `ADD COLUMN embedding vector(768)`. All existing rows have `embedding = NULL` after migration.
4. `DROP INDEX chunks_embedding_hnsw_idx`.
5. `CREATE INDEX chunks_embedding_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)`.
6. Re-enqueue the `embed` stage for every document with `pipeline_status='ready'`. (Use an explicit `INSERT INTO ingestion_jobs` ... `WHERE pipeline_status='ready'`).
7. `downgrade()`: raises `NotImplementedError` with a clear message ("Phase 0 schema break is one-way; restore from a pre-upgrade backup.").

### MCP / API Surface
**No tool signatures change in Phase 0.** Output shapes are unchanged. Behavioral change is invisible to MCP callers — they just get better hits.

### Retrieval-Eval Gate
```bash
sweep --mode retrieval-eval --run-id eval-fixture-v1 \
  --api-base http://localhost:8100 \
  --label phase-0-granite-r2
```
**Pass criteria**: overall recall@10 improvement ≥ +0.08; no per-corpus regression > -0.05; nDCG@10 should also improve.

### Risks
- **Granite-R2 was released April 2026** — only 18K downloads/month at time of plan. Real-world bugs may surface. Mitigation: keep e5-small swap-back available via env var until Phase 1 lands; document the rollback in the PR.
- **Re-embed time on prod corpus** — sweep baselines exist for ~10K docs, prod corpora may be larger. Plan re-embed as a maintenance window.
- **Reranker is a new service** — adds health-check surface, restart-coordination on macOS. The "Quit hang" pattern in [project_menubar_process_management_audit.md](https://github.com/alex/mcp-gateway/blob/main/memory/project_menubar_process_management_audit.md) might bite again. Add the reranker to the Tier-A fixes if they aren't already shipped.

---

## Phase 1: Late Chunking + Multi-Granularity + Heading Chain

### Goal
1. Change the embed stage to embed the full doc once (Granite-R2 supports 32K tokens), then mean-pool token reps into per-chunk vectors. Carries cross-chunk context into each chunk embedding.
2. Add two new embedding tables: `section_embeddings` (one per heading, embedded from heading + surrounding ~5K tokens) and `document_embeddings` (one per doc, embedded from summary + first ~2K tokens).
3. Make `kb_read_passages` return the heading chain alongside each passage so the LLM sees structural context for free.

### File Structure
**Create:**
- `src/harbor_clerk/worker/stages/late_chunk_embed.py` — New embed-stage variant doing late chunking. May replace `embed.py` outright (flagged by `embed_strategy` setting).
- `src/harbor_clerk/models/section_embedding.py`, `document_embedding.py` — SQLAlchemy models.
- `alembic/versions/0024_section_doc_embeddings_and_late_chunking.py` — Migration: new tables + HNSW indexes; bump `upgrade_phase=1`.
- `src/harbor_clerk/search.py` — Extend hybrid search to accept `granularity: list[Literal['chunk', 'section', 'document']]`; ensemble via RRF across the requested layers.
- `tests/test_late_chunking.py` — End-to-end: docs embedded via late chunking produce different (better) embeddings on cross-reference queries vs naive chunking.
- `tests/test_multi_granularity_search.py` — RRF merge produces correct ordering; granularity filter works.

**Modify:**
- `src/harbor_clerk/worker/pipeline.py` — Section + doc embedding stages added to `_PARALLEL_STAGES`; depend on `chunk`.
- `src/harbor_clerk/api/schemas/search.py` — Add `granularity` field to `SearchRequest`.
- `src/harbor_clerk/api/routes/search.py` — Pass through to hybrid search.
- `src/harbor_clerk/mcp_server.py` — `kb_search` accepts `granularity` param; `kb_read_passages` includes `heading_chain: list[str]` in each passage payload.
- `embedder/src/embedder/app.py` — Add `POST /embed_late_chunk` endpoint accepting full doc text + chunk char-offset boundaries, returning per-chunk pooled embeddings.

### Schema Migration
`0024_section_doc_embeddings_and_late_chunking.py`:
- `CREATE TABLE section_embeddings (section_id UUID PK, doc_id UUID FK, heading_id UUID FK, embedding vector(768), char_start INT, char_end INT)`.
- `CREATE TABLE document_embeddings (doc_id UUID PK FK, embedding vector(768), source TEXT CHECK source IN ('summary', 'first_chunk', 'combined'))`.
- HNSW indexes on both.
- Update `schema_metadata`: `upgrade_phase='1'`.
- Re-enqueue `embed` stage for all docs to populate the new tables AND to redo chunk embeddings with late chunking.

### MCP / API Surface
- `kb_search` gains `granularity` param (defaults to `['chunk']` for backward compat). When set to `['chunk', 'section', 'document']`, results are RRF-merged across all three.
- `kb_read_passages` response gains `heading_chain` field per passage: `["Contract", "Section 4", "4.2 Termination"]`.
- All other tools unchanged.

### Retrieval-Eval Gate
```bash
sweep --mode retrieval-eval --run-id eval-fixture-v1 \
  --label phase-1-late-chunking --prior-label phase-0-granite-r2
```
**Pass criteria**: overall recall@10 improvement ≥ +0.03; no per-corpus regression > -0.03; LongEmbed-style queries (multi-paragraph, cross-reference) should show ≥+0.05.

### Risks
- **Section embedding granularity** is ambiguous — chunks span headings, headings can be 50 chars or 50 pages. The brainstorm needs to pick: every heading? only level 1-2? smart packing into ~5K-token sections? Default to a `_split_into_sections()` helper that targets 3-5K tokens per section, respecting heading boundaries.
- **Memory cost**: 3× embedding rows. For a corpus with 500K chunks + ~50K sections + 10K docs, that's 560K vectors of 768 floats = ~1.7 GB raw. pgvector with HNSW will roughly double that. Plan for ~4 GB Postgres memory hit; verify on prod-scale Mac mini.
- **Late chunking implementation** depends on sentence-transformers' token-offset preservation; verify with Granite-R2 specifically before committing.

---

## Phase 2: Metadata Layer (Doc-Type, Temporal, PII, Tables)

### Goal
Layer four orthogonal preprocessing signals onto every ingested doc so the LLM can filter and route via MCP without post-hoc work:
1. **Doc-type classification** — one of {contract, invoice, email, memo, policy, report, presentation, form, other} per doc.
2. **Temporal extraction** — `documents.dated_at`, `documents.deadline_at`, plus per-chunk `temporal_anchors` table.
3. **PII tagging** — Microsoft Presidio detects PII; per-chunk `pii_types` column.
4. **Table-aware extraction** — for PDF/XLSX, extract tables as markdown into a `document_tables` table separate from flowing text.

### File Structure
**Create:**
- `src/harbor_clerk/worker/stages/classify.py` — Doc-type classifier worker stage.
- `src/harbor_clerk/worker/stages/temporal.py` — Date extraction worker stage.
- `src/harbor_clerk/worker/stages/pii.py` — Presidio PII tagging worker stage.
- `src/harbor_clerk/worker/stages/tables.py` — Table extraction (camelot for PDF, openpyxl for XLSX) worker stage.
- `src/harbor_clerk/models/document_table.py`, `temporal_anchor.py` — Models.
- `src/harbor_clerk/llm/doc_type_classifier.py` — Small local LLM (Qwen 4B?) or fine-tuned distilbert; few-shot prompt with the 9 categories.
- `alembic/versions/0025_metadata_layer.py` — Migration: new columns (`documents.doc_type`, `documents.dated_at`, `documents.deadline_at`, `chunks.pii_types`), new tables (`document_tables`, `temporal_anchors`); bump `upgrade_phase=2`.
- `tests/test_doc_type_classifier.py`, `test_temporal_extraction.py`, `test_pii_tagging.py`, `test_table_extraction.py`.

**Modify:**
- `src/harbor_clerk/api/schemas/search.py` — Add `doc_type: list[str] | None`, `dated_after: date | None`, `dated_before: date | None`, `redact_pii: bool = False` to `SearchRequest`.
- `src/harbor_clerk/api/routes/search.py` — Apply filters; if `redact_pii=True`, redact chunk text before returning.
- `src/harbor_clerk/search.py` — Pass filters through to the hybrid query.
- `src/harbor_clerk/mcp_server.py` — `kb_search` accepts new filters; new tool `kb_list_doc_types` returns `[{doc_type, count}]` so the LLM can discover what's filterable.
- `src/harbor_clerk/worker/pipeline.py` — Add the four new stages to `_PARALLEL_STAGES`.

### Schema Migration
`0025_metadata_layer.py`:
- `ALTER TABLE documents ADD COLUMN doc_type TEXT, ADD COLUMN dated_at DATE, ADD COLUMN deadline_at DATE`.
- `ALTER TABLE chunks ADD COLUMN pii_types TEXT[]`.
- `CREATE TABLE document_tables (table_id UUID PK, doc_id UUID FK, page_num INT, markdown TEXT, position INT)`.
- `CREATE TABLE temporal_anchors (anchor_id UUID PK, chunk_id UUID FK, doc_id UUID FK, date_value DATE, surface_form TEXT, char_start INT, char_end INT)`.
- Indexes on `documents.doc_type`, `documents.dated_at`, `chunks.pii_types` (GIN for array).
- Update sentinel `upgrade_phase='2'`.
- For each existing doc, enqueue the four new stages so back-population happens via the normal pipeline.

### MCP / API Surface
- `kb_search` accepts: `doc_type=['contract']`, `dated_after='2026-01-01'`, `dated_before='2026-06-30'`, `redact_pii=true`.
- New tool: `kb_list_doc_types` — discover what doc_types exist in the corpus.
- All existing tools unchanged.

### Retrieval-Eval Gate
The retrieval-eval harness uses the corpus's existing baseline questions which don't exercise the new filters. **Run two evals**:
1. Standard retrieval-eval (recall@10, no filter changes) — pass criteria: no regression worse than -0.02. The metadata layer should not affect plain hybrid retrieval.
2. **Filter precision eval** — write a small new fixture: ~20 questions per corpus that are answerable only by docs of a specific doc_type or date range. Compare top-10 with vs without filters. Pass criteria: filtered precision@10 ≥ unfiltered precision@10 + 0.20 on this fixture.

This is the first phase where retrieval-eval alone isn't enough; add a `--mode filter-precision-eval` if the fixture warrants its own harness.

### Risks
- **Doc-type classifier model choice** depends on what the brainstorm picks. Local LLM via existing `llama-server` is cheapest but adds latency. Fine-tuned distilbert is faster but needs training data — and CUAD/Enron/synthetic may be enough for that.
- **Presidio English-only by default** — French PII detection needs the right spaCy pipeline; coordinate with the [Language Packs on Demand](https://github.com/alex/mcp-gateway/blob/main/memory/project_language_packs_on_demand.md) plan.
- **Camelot has heavy deps** (Ghostscript on Linux, libffi). Verify both Docker and macOS bundling.

---

## Phase 3: Knowledge Graph (Canonicalization + Relations + Cross-Doc Linking)

### Goal
Turn the existing flat `entities` table into a queryable graph:
1. **Canonicalize** mentions across docs (Acme Inc. = Acme Corporation = ACME).
2. **Extract relations** with GLiNER-Relex (zero-shot typed SVO triples).
3. **Cross-document linking** — resolve "see contract dated 2024-03-15" to the actual doc_id.
4. **5 new MCP tools** for LLM graph queries.

### File Structure
**Create:**
- `src/harbor_clerk/worker/stages/canonicalize_entities.py` — Cluster entity mentions per type, pick canonical names.
- `src/harbor_clerk/worker/stages/relations.py` — GLiNER-Relex extraction stage.
- `src/harbor_clerk/worker/stages/cross_doc_links.py` — Regex + resolver for inter-doc references.
- `src/harbor_clerk/models/entity_canonical.py`, `entity_relation.py`, `document_reference.py` — Models.
- `src/harbor_clerk/graph/canonicalize.py` — Pure clustering logic (testable in isolation).
- `src/harbor_clerk/graph/relations_extractor.py` — Wraps GLiNER-Relex.
- `src/harbor_clerk/graph/resolver.py` — Inter-doc reference resolution.
- `alembic/versions/0026_knowledge_graph.py` — Migration: new tables, FKs from `entities.canonical_id`; bump `upgrade_phase=3`.
- `tests/test_canonicalize.py`, `test_relations_extractor.py`, `test_cross_doc_resolver.py`, `test_mcp_graph_tools.py`.

**Modify:**
- `src/harbor_clerk/mcp_server.py` — Add 5 new tools: `kb_entity_timeline`, `kb_entity_neighborhood`, `kb_paragraph_coincidence`, `kb_cross_corpus_match`, `kb_document_references`. Update `kb_entity_*` existing tools to use canonical entities by default with `include_aliases=true` opt-out.
- `src/harbor_clerk/worker/pipeline.py` — Add the three new stages; `canonicalize_entities` runs corpus-wide on a schedule, not per-doc.

### Schema Migration
`0026_knowledge_graph.py`:
- `CREATE TABLE entity_canonical (canonical_id UUID PK, entity_type TEXT, canonical_name TEXT, aliases TEXT[], mention_count INT, first_seen_at TIMESTAMPTZ, last_seen_at TIMESTAMPTZ)`.
- `ALTER TABLE entities ADD COLUMN canonical_id UUID REFERENCES entity_canonical(canonical_id)`.
- `CREATE TABLE entity_relations (relation_id UUID PK, subject_id UUID FK→entity_canonical, predicate TEXT, object_id UUID FK→entity_canonical, doc_id UUID FK, chunk_id UUID FK, confidence FLOAT)`.
- `CREATE TABLE document_references (reference_id UUID PK, source_doc_id UUID FK, target_doc_id UUID FK NULL, mention_text TEXT, char_start INT, char_end INT, anchor_date DATE NULL, resolution_status TEXT CHECK IN ('resolved', 'ambiguous', 'unresolved'))`.
- Indexes on `(canonical_id)`, `(subject_id, predicate)`, `(object_id, predicate)`, `(source_doc_id)`, `(target_doc_id)`.
- Update sentinel `upgrade_phase='3'`.

### MCP / API Surface
Five new tools. Tool descriptions and JSON schemas TBD in the phase 3 detailed plan. Skeleton:

| Tool | Purpose | Input |
|---|---|---|
| `kb_entity_timeline` | All docs mentioning entity X, ordered by date | `{entity_canonical_id, since?, until?}` |
| `kb_entity_neighborhood` | Entities most strongly co-occurring with X (1-hop graph) | `{entity_canonical_id, top_n}` |
| `kb_paragraph_coincidence` | Docs where X and Y appear in the same chunk | `{entity_a_id, entity_b_id}` |
| `kb_cross_corpus_match` | Entities in this doc that also appear in any doc of type Y | `{doc_id, other_doc_type}` |
| `kb_document_references` | Forward + backward inter-doc reference graph for a doc | `{doc_id, direction='both'/'forward'/'backward'}` |

### Retrieval-Eval Gate
- Standard retrieval-eval (no regression worse than -0.02 on baseline questions).
- New "multi-hop fixture": ~30 hand-crafted questions per corpus that require entity-graph traversal ("which contracts involve any party that also appears in invoices from 2024?"). Pass criteria: top-10 includes the correct doc set on ≥ 60% of fixture questions. This fixture is generated once during the phase 3 brainstorm and committed.

### Risks
- **Canonicalization is the hard problem.** "John Smith" the lawyer vs "John Smith" the witness are different entities the model can't distinguish without context. The brainstorm needs to pick a clustering strategy and confidence threshold; ship with a manual override UI is worth considering.
- **GLiNER-Relex zero-shot accuracy on office docs is unproven** at this scale. Validation: run on the CUAD subset, manually grade 50 extracted triples per doc_type, gate on ≥70% precision.
- **`document_references` regex resolver** will be noisy. Plan for the `resolution_status='ambiguous'` bucket to be the dominant case for free-form mentions like "the earlier agreement."

---

## Phase 4: Propositional Indexing + HippoRAG-Style Multi-Hop

### Goal
1. Decompose each chunk into self-contained atomic factoids via a local LLM pass; embed those into a `propositions` table.
2. Expose `kb_find_fact` MCP tool for direct factoid retrieval.
3. Implement HippoRAG-style multi-hop search: personalized PageRank over the entity graph (Phase 3 output) seeded by the query's entities, returning the top chunks via graph traversal. Expose as `kb_multihop_search`.

### File Structure
**Create:**
- `src/harbor_clerk/worker/stages/propositions.py` — Per-chunk LLM extraction stage.
- `src/harbor_clerk/llm/proposition_extractor.py` — Prompt template + parsing for the LLM call.
- `src/harbor_clerk/graph/hipporag.py` — Pure personalized-PageRank impl over `entity_canonical` × `entity_relations`.
- `src/harbor_clerk/models/proposition.py` — Model.
- `alembic/versions/0027_propositions.py` — Migration.
- `tests/test_proposition_extractor.py`, `test_hipporag.py`, `test_mcp_find_fact.py`, `test_mcp_multihop_search.py`.

**Modify:**
- `src/harbor_clerk/mcp_server.py` — Add `kb_find_fact` and `kb_multihop_search` tools.
- `src/harbor_clerk/worker/pipeline.py` — Add propositions stage to `_PARALLEL_STAGES`.

### Schema Migration
`0027_propositions.py`:
- `CREATE TABLE propositions (proposition_id UUID PK, chunk_id UUID FK, doc_id UUID FK, text TEXT, embedding vector(768), extracted_at TIMESTAMPTZ)`.
- HNSW index on `embedding`.
- Update sentinel `upgrade_phase='4'`.

### MCP / API Surface
- `kb_find_fact` — input `{query, k}`, returns top-K propositions ranked by semantic similarity, each with a chunk_id + doc_id citation.
- `kb_multihop_search` — input `{query, max_hops=2}`, returns chunks reached via PageRank traversal of the entity graph seeded by entities in the query.

### Retrieval-Eval Gate
- Standard retrieval-eval: no regression > -0.02.
- "Fact-extraction subset" — questions in the baseline of the form "what is X" / "when did Y happen" — measure recall@5 of `kb_find_fact` against baseline citations. Pass criteria: ≥ +0.10 vs the Phase 3 `kb_search` recall on the same subset.
- "Multi-hop subset" — the Phase 3 multi-hop fixture run through `kb_multihop_search`. Pass criteria: ≥ +0.15 over Phase 3 baseline on this fixture.

### Risks
- **LLM-extraction cost over the full corpus** is the big-ticket item. ~500K chunks × 1-2 LLM calls each on a local Qwen 4B = 50-100 hours single-threaded. Plan re-extraction as a maintenance background task. Alternative: only extract for docs flagged via a "high-value-doc-type" filter from Phase 2.
- **HippoRAG implementation details** (damping factor, max iterations, edge weighting) are tunable. Defaults from the paper are a starting point; expect to need a hand-tuned config for the office-doc domain.

---

## Phase 5: UI (Graph Viz, Topic Projection, Doc-Type Composition, Retrieval Inspector)

### Goal
Wire the data from phases 0-4 into the existing Explore + Search pages, plus a new "inspector" overlay on search results. Use the existing `d3-force`, `d3-drag`, `d3-scale`, `d3-selection` deps (already pulled in but not used for graph viz). No new backend.

### File Structure
**Create:**
- `frontend/src/components/explore/EntityGraph.tsx` — Force-directed graph using d3-force, nodes = canonical entities, edges = relations from Phase 3.
- `frontend/src/components/explore/TopicProjection.tsx` — 2D scatter of docs colored by topic, using UMAP coords from BERTopic (already computed but not surfaced).
- `frontend/src/components/explore/DocTypeComposition.tsx` — Treemap/pie of corpus by doc_type (from Phase 2).
- `frontend/src/components/search/RetrievalInspector.tsx` — Per-result expandable section showing FTS score, vector score, reranker score, matched entities.
- `frontend/src/components/explore/EntityTimeline.tsx` — Bar chart of entity mention frequency over time (uses Phase 2 `temporal_anchors`).
- Tests for each new component (use Vitest if it's set up; otherwise React Testing Library).

**Modify:**
- `frontend/src/pages/ExplorePage.tsx` — Replace the three flat top-N entity lists with `<EntityGraph />`; add `<TopicProjection />` and `<DocTypeComposition />` sections.
- `frontend/src/pages/SearchPage.tsx` — Wrap each hit row with `<RetrievalInspector />`.
- `frontend/src/api/explore.ts` (or wherever) — Add fetches for the new Phase 3 endpoints powering the graph.
- `src/harbor_clerk/api/routes/explore.py` (might need creating) — Endpoints returning canonical-entity graph data + UMAP topic coords + retrieval-score breakdown per hit.

### Schema Migration
None.

### Retrieval-Eval Gate
Not retrieval-gated. UX-gated:
- Run through the existing user flows; verify each new viz renders without errors with the prod-scale corpus.
- Measure render time: graph with 5K nodes should be interactive; treemap with 9 categories should be instant.

### Risks
- **Graph with thousands of entities** will need either client-side aggregation (top-N + "show more") or server-side filtering. d3-force chokes past ~2-3K nodes in practice.
- **UMAP coords currently get thrown away** — need to plumb them through the `corpus_topics` model + API + frontend.
- **Tailwind 4 + d3** integration — d3 manipulates the DOM directly; React + d3 needs careful useEffect/useRef discipline. Use a wrapper pattern (single `<div ref={containerRef}>`, d3 mounts inside).

---

## Inter-Phase Gating Protocol

Between each phase:

1. **Run the retrieval-eval gate** as specified in the phase's "Retrieval-Eval Gate" section.
2. **Capture the gate output** (the `metrics.csv` and `summary.json` from the eval) in the phase's PR description as a code block.
3. **If pass**: tag the eval label permanently as `phase-N-final-<short-name>` so subsequent phases can `--prior-label` against it for cumulative comparison.
4. **If fail**: do not merge. Either:
   - Revise the phase implementation (most common).
   - Identify which sub-feature regressed and roll it back, re-eval.
   - In rare cases, conclude the phase's premise was wrong (e.g. Phase 1 late chunking doesn't help on your corpus) — write up the negative result in a memory file and revise the plan.

5. **Ablation discipline**: do NOT bundle phases. If Phase 0 + Phase 1 land together you'll never know which one was load-bearing for the recall delta. Each phase's eval label tells the upgrade story.

6. **PR followups discipline**: any out-of-scope work surfaced during phase implementation goes into `pr_followups.md` AND the PR description. Phases 1-5 each typically generate 2-5 followups.

---

## Final Eval (After All 6 Phases)

After Phase 5 merges:

1. **Full sweep re-run** with the upgraded pipeline (all 8 local models × all 3 corpora × phase-5 retrieval stack). This is the "definitive evaluation" the user has been planning for.
2. **Cumulative retrieval-eval**: `--label final --prior-label embedding-v2`. NOTE: there is no e5-small reference run — the box was cut over before retrieval-eval existed (see the eval-corpus spec §6), so the cumulative delta is measured against the `embedding-v2` reference, not e5-small. The "+0.20–0.35 vs the original baseline" target is therefore not directly measurable; judge the absolute final recall@10 and the sum of per-phase deltas instead.
3. **Filter-precision eval**: re-run the Phase 2 fixture to confirm filters still work end-to-end.
4. **Multi-hop fixture**: re-run the Phase 3 fixture through `kb_multihop_search` (Phase 4 endpoint). Confirm the multi-hop gains held.
5. **LLM-as-judge pairwise eval**: pick 50-100 representative questions from the baseline; have a frontier LLM (a DIFFERENT one from the baseline generator, to avoid self-preference) compare baseline-pipeline answers vs upgrade-pipeline answers; report win-rate.
6. **Write the milestone memory file** capturing: cumulative deltas, what worked, what didn't, what got cut, what's deferred. This becomes the canonical "what shipped in the upgrade" document.

---

## Open Questions Per Phase (to resolve at brainstorm time)

These are the decisions to make BEFORE writing the per-phase detailed plan. Each item is a concrete question with no current answer.

### Phase 0 — Brainstorm Questions
- Reranker deployment: separate service or co-hosted in embedder container?
- Reranker pool size (how many candidates to rerank per query): 50? 100?
- macOS service-management strategy for the reranker — new launchd plist or subprocess of HarborClerkServer?
- Granite-R2 download: bake into Docker image (~700MB) or download on first run?
- Re-embed strategy for prod corpus: blocking maintenance window vs background trickle?

### Phase 1 — Brainstorm Questions
- Section-embedding boundaries: every heading? level 1-2? smart packing to 3-5K tokens?
- Late-chunking implementation: does Granite-R2 + sentence-transformers preserve token offsets cleanly? If not, fall back to a custom encoder call.
- Multi-granularity ensemble weights for RRF: equal? tuned per query type?
- Do we keep the chunk-level embedding column or migrate fully to per-table embeddings?

### Phase 2 — Brainstorm Questions
- Doc-type classifier: fine-tuned distilbert (needs training data) vs few-shot local LLM (slower but no training)?
- Doc-type categories: the 9 I listed, or pull from a customer-facing taxonomy?
- Temporal extraction: spaCy + rules sufficient, or use HeidelTime/SUTime?
- Presidio + French: which spaCy model pipeline, and how does it integrate with the [language packs] effort?
- Table extraction strategy for native PDF vs scanned PDF (the latter is image-only post-OCR)?

### Phase 3 — Brainstorm Questions
- GLiNER vs spaCy NER: full replacement or augmentation?
- Canonicalization confidence threshold: HDBSCAN min_cluster_size, contextual-embedding window?
- Relation types per doc-type: enumerate up front in the brainstorm.
- Manual canonicalization override UI: in scope here or deferred to phase 5?
- Run canonicalization corpus-wide on a schedule, or incrementally per-doc?

### Phase 4 — Brainstorm Questions
- Proposition extractor model: Qwen 4B local, or use the active LLM from `model_settings`?
- Extraction prompt: zero-shot vs few-shot with corpus-specific examples?
- HippoRAG tunables: damping, max iterations, edge weighting?
- Update strategy: re-extract propositions when a chunk's embedding changes, or never re-extract?

### Phase 5 — Brainstorm Questions
- Graph library: pure d3-force, or a React wrapper (react-d3-graph, vis-network)?
- Node aggregation strategy past ~2K entities: top-N filter, hierarchical clustering view?
- Retrieval inspector: per-result expandable, or a separate "explain this result" modal?
- Topic projection layout: scatter only, or also temporal animation?

---

## Branch + Memory Setup (do this first)

Before starting Phase 0:

1. **Create a tracking memory file**: `project_retrieval_mcp_upgrade.md` with a status checklist (one row per phase, status pending/in-progress/done with PR link).
2. **Create the worktree**: `cd ~/mcp-gateway && git worktree add .claude/worktrees/retrieval-upgrade-phase-0 -b feat/retrieval-phase-0-granite main`.
3. **Build the `eval-fixture-v1` corpus + baselines** before running any retrieval-eval gate — see `docs/superpowers/plans/2026-05-21-eval-fixture-setup.md`. Do NOT run the model-comparison sweep on the eval box (it re-ingests and destroys the fixture).
4. **Verify the master plan is committed**: this document should be on `main` (via its own PR) before phase 0 starts, so the per-phase plans can link back to it.

---

## Self-Review Notes

This master plan deliberately omits bite-sized step-by-step instructions because each phase is large enough to be its own detailed implementation plan. The information level here matches what's needed for project planning + brainstorm input, not for direct execution.

**Cross-references:**
- The `--mode retrieval-eval` harness this plan depends on shipped in this session under `scripts/test_corpora/runner/retrieval_eval.py`.
- The eval fixture (corpus + baselines) the gate runs against is specified in `docs/superpowers/specs/2026-05-21-retrieval-eval-corpus-design.md` and built via `docs/superpowers/plans/2026-05-21-eval-fixture-setup.md`. (The `2026-05-05-prod` sweep — see `memory/project_sweep_2026_05_05_prod_resume.md` — is a separate model-comparison effort; its baselines are NOT used by the gate.)
- The research grounding for each phase is in this session's transcript.

**Things explicitly NOT in this plan (descoped):**
- ColBERT-style late interaction (out: bge-reranker captures the gain at lower cost).
- HyDE / query rewriting (out: frontier LLMs are the caller, they decompose themselves).
- Apache AGE / graph DB (out: plain Postgres tables suffice for the queries we care about).
- "GraphRAG"-style full LLM-extracted entity graph (out: too expensive, GraphRAG-Bench 2026 showed marginal gains).
- jina-embeddings-v3/v4/v5 (out: CC-BY-NC license incompatible with redistributable appliance).
- Apple AGE / Memgraph / Neo4j (out: dependency minimalism wins for a single-tenant appliance).
- Claim/argument extraction (out: defer until a legal-customer needs it).

**Things explicitly deferred (will land separately):**
- Per-doc-type aspect extraction (invoices, contracts) — wait for Phase 2 doc-type classifier, then build per-type as a separate effort.
- Mobile/responsive ExplorePage — Phase 5 targets desktop first.
- Multi-user / collaborative annotation of canonical entities — single-tenant scope keeps this out.
