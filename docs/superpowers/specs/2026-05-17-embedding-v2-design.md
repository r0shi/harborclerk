# Embedding v2 (Granite-R2 + Reranker + Schema Rebase) — Design

**Status:** Design / pre-plan
**Spec author session date:** 2026-05-17
**Companion documents:**
- Master staging plan: [`docs/superpowers/plans/2026-05-17-retrieval-mcp-upgrade-master-plan.md`](../plans/2026-05-17-retrieval-mcp-upgrade-master-plan.md) (this is the first of 6 phases)
- Implementation plan (to be written next): `docs/superpowers/plans/2026-05-17-embedding-v2-implementation.md`

## Goal

Swap Harbor Clerk's embedding stack from `intfloat/multilingual-e5-small` (118M params / 384-dim) to `ibm-granite/granite-embedding-311m-multilingual-r2` (311M / 768-dim), add `BAAI/bge-reranker-v2-m3` as a cross-encoder precision stage on top of hybrid retrieval, and rebase the alembic chain so the new initial migration represents the new schema directly.

Expected impact: overall recall@10 improvement of +0.08 to +0.18 against the `2026-05-05-prod` sweep baselines, gated by `--mode retrieval-eval --label embedding-v2`.

## Non-Goals

- **Late chunking** (embedding the full doc and pooling contextualized token reps) — deferred to the next phase.
- **Multi-granularity embeddings** (separate section/document tables) — next phase.
- **Heading chain in passages** — next phase.
- **MCP tool surface changes** — none in this phase. `kb_search`, `kb_read_passages`, etc. keep identical request/response shapes. The only response change is internal: `score` becomes the reranker score and a new optional `score_breakdown` field is populated (consumed by the Phase 5 retrieval inspector, not by current clients).
- **In-app destructive migration of existing DBs** — explicitly out of scope. The alembic chain is rebased; existing DBs either get wiped and re-ingested (recommended for office appliances with watched folders) or migrated via the external script.
- **Provider abstraction** (`EmbeddingProvider` ABC, model registry, etc.) — YAGNI. Add when a second model is worth switching between.

## Architecture

```
                                        ┌──────────────────────┐
                                        │  Embedder service    │
                                        │  port 8000           │
                                        │  Granite-R2 768-dim  │
                                        └──────────▲───────────┘
                                                   │ POST /embed
                                                   │ (HTTP)
┌────────────────┐    POST /api/search             │
│ MCP client     ├───┐                             │
│ (frontier LLM) │   │   ┌─────────────────────────┴───────┐
└────────────────┘   ├──▶│  HC API (FastAPI)               │
┌────────────────┐   │   │  ────────────────────────────── │
│ Web UI         ├───┘   │  on boot: verify_schema_sentinel│
└────────────────┘       │  /api/search:                   │
                         │   1. hybrid_search(K + pad)     │
                         │   2. rerank top pool            │───┐
                         │   3. truncate to K              │   │
                         └─────────────────────────────────┘   │ POST /rerank
                                                               │ (HTTP)
                                                   ┌───────────▼──────────┐
                                                   │  Reranker service    │
                                                   │  port 8001           │
                                                   │  bge-reranker-v2-m3  │
                                                   └──────────────────────┘
```

- Embedder service: existing FastAPI process at `embedder/`, only change is the bundled model + removal of the e5 query/passage prefix logic. Used by the `embed` worker stage at ingest time.
- Reranker service: NEW FastAPI process modeled after embedder. Loaded with `BAAI/bge-reranker-v2-m3` at startup. Used by `/api/search` at query time (and by the future `kb_search` MCP tool — same code path).
- HC API: same shape as today, with a new `rerank_hits()` helper called between hybrid merge and top-K truncation.
- Schema sentinel: new `schema_metadata` table populated by the rebased initial migration; queried at boot by both API and workers.

## Components

### 1. Embedder service (model swap)

**Existing:** [`embedder/src/embedder/app.py`](../../../embedder/src/embedder/app.py) — FastAPI app loading a SentenceTransformer model and exposing `POST /embed`.

**Changes:**
- Default `EMBED_MODEL` becomes `ibm-granite/granite-embedding-311m-multilingual-r2`.
- The `task: "query" | "passage"` request param is **accepted but ignored** — Granite-R2 uses CLS pooling and needs no e5-style prefix. The param stays in the schema so existing callers (the `embed` worker stage at `src/harbor_clerk/worker/stages/embed.py`) work unchanged.
- Response shape unchanged (`{embeddings: list[list[float]], model: str, dimensions: int}`); `dimensions` now reports `768`.
- Embedding precision: fp16 (sentence-transformers default; works on MPS).
- Docker image: bake the Granite-R2 weights at build time via `huggingface-cli download` step. Adds ~700 MB to the embedder image.
- macOS bundle: download into the app bundle's `Resources/models/` during `make` via `macos/scripts/download-models.sh`.

### 2. Reranker service (new)

**New file:** `embedder/src/embedder/reranker.py` (sharing pyproject with embedder so both ship in the same wheel).

**Wire shape:**
```
POST /rerank
{
  "query": "what is the termination clause",
  "passages": [
    "Title: ACME-Beta MSA\n\nChunk: 4.2 Either party may terminate ...",
    "Title: ACME-Beta MSA\n\nChunk: 7.1 Confidentiality survives ...",
    ...
  ],
  "top_k": 10
}

→ 200 OK
{
  "scores": [
    {"index": 0, "score": 0.9821},
    {"index": 1, "score": 0.4733},
    ...
  ],
  "model": "BAAI/bge-reranker-v2-m3"
}
```

- `scores` is sorted desc and truncated to `top_k`.
- `index` is the position in the input `passages` array — the caller correlates back to its full hit objects.
- Empty `passages` → empty `scores`, 200 OK (not an error).
- `top_k > len(passages)` → returns all available, 200 OK.

**Implementation:** `sentence_transformers.CrossEncoder("BAAI/bge-reranker-v2-m3")`. fp16. Health endpoint at `/health` returns 200 when model is loaded.

**Service port:** 8001 (Docker), configurable port (macOS, allocated by HarborClerkServer alongside the existing API/embedder ports).

**Dockerfile:** new `docker/reranker.Dockerfile`. Same Python base as embedder. Bakes the reranker weights at build time.

### 3. Schema sentinel + boot-time verification

**New table** (created by the rebased initial migration):
```sql
CREATE TABLE schema_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO schema_metadata (key, value) VALUES
  ('embed_model', 'granite-embedding-311m-multilingual-r2'),
  ('embed_dim', '768'),
  ('reranker', 'bge-reranker-v2-m3');
```

No `upgrade_phase` row — `alembic_version` already records what version of the schema the DB is at. The sentinel exists specifically to assert "what model + dim is this DB compatible with."

**New file:** `src/harbor_clerk/db_health.py`

```python
async def verify_schema_sentinel(session: AsyncSession) -> None:
    """Panic-exit if the DB's schema_metadata doesn't match this binary's settings.

    Called at API lifespan startup AND at worker boot, before any other DB work.
    Failure mode: log CRITICAL message naming the expected vs actual values,
    then sys.exit(2). No auto-migration, no fallback.
    """
    ...
```

The check compares:
- `schema_metadata.embed_model` vs `settings.embed_model`
- `schema_metadata.embed_dim` vs `settings.embed_dim`

On mismatch, the log message is multi-line and explicit:
```
CRITICAL: schema sentinel mismatch — refusing to start.
  expected: embed_model=granite-embedding-311m-multilingual-r2, embed_dim=768
  found:    embed_model=multilingual-e5-small, embed_dim=384
This binary requires the embedding-v2 schema. Either:
  1. Drop and recreate the database (fresh schema, then re-ingest via watched folders), OR
  2. Run scripts/migrate_to_embedding_v2.py against this DB.
See docs/upgrade-runbook.md#embedding-v2 for details.
```

If the `schema_metadata` table itself is missing (pre-rebase DB): same panic, with a slightly different message naming the missing table.

**Wired into:**
- `src/harbor_clerk/api/app.py` lifespan startup — runs before the API serves traffic.
- `src/harbor_clerk/worker/entry.py` `main()` — runs before the worker pulls its first job.

### 4. Search reranking integration

**New file:** `src/harbor_clerk/search_rerank.py`

```python
async def rerank_hits(query: str, hits: list[SearchHit], top_k: int) -> list[SearchHit]:
    """Rerank a candidate pool via the reranker service, truncate to top_k.

    On reranker HTTP failure: if settings.reranker_strict, raise; otherwise
    log a warning and return hits[:top_k] in their original (hybrid-merged)
    order. Sets `hit.score` to the reranker score on success.
    """
    ...
```

The passage strings sent to the reranker are formatted as:
```
Title: {doc_title}\n\nChunk: {chunk_text}
```
Pure chunk text loses cross-encoder context; including the doc title gives the reranker enough signal to score correctly on chunks that reference parent context implicitly ("this section discusses...").

**Modified:** `src/harbor_clerk/search.py`

The `hybrid_search()` function gains an optional rerank step at the end:
1. Fetch top `K + reranker_top_k_pad` from the merged hybrid pool (instead of just K).
2. If `settings.reranker_enabled`, call `rerank_hits(query, pool, K)`; replace `hit.score` with the reranker score; sort desc.
3. Otherwise (or after fallback), truncate to top K by combined hybrid score (current behavior).

The new `score_breakdown` field on `SearchHitOut` is populated with `{fts, vector, hybrid, reranker}`. Always present in the API response (typed `dict | None`). Not consumed by any client in this phase — wired for the Phase 5 retrieval inspector.

### 5. macOS service-management

The reranker is a new managed Python subprocess. Same pattern as the embedder:

**New Swift files** (under `macos/HarborClerkServer/HarborClerkServer/`):
- `RerankerService.swift` — service definition matching `EmbedderService.swift`.

**Modified Swift files:**
- `ServiceManager.swift` — register reranker in the service list; startup ordering: Postgres → Tika → Embedder → Reranker → API → Workers.
- `PreferencesWindow.swift` / `Settings.swift` — expose reranker port + enabled toggle alongside embedder.
- `ServiceLogsPage.tsx` (frontend) — list reranker among the services whose logs are tailed.
- `SystemStatusPage.tsx` (frontend) — show reranker health alongside embedder.

**NOT launchd.** Launchd is the right tool for Postgres + Tika (long-running, expensive to restart, OS-integrated). The reranker is cheap to restart, benefits from being killed/restarted alongside the rest of the Python services on model switches or upgrades, and matches the embedder's lifecycle.

### 6. Alembic rebase

**The 22 existing alembic migrations** (`alembic/versions/0001_initial_schema.py` through `0022_*.py`) are **deleted** from the repo and **replaced by one new file**: `alembic/versions/0001_initial.py`.

The new initial migration:
- Creates the entire schema in its current shape (every table, every column, every constraint, every index, every CHECK), **PLUS** the embedding-v2 changes:
  - `chunks.embedding` is `vector(768)` (not 384)
  - `schema_metadata` table created and populated with the three sentinel rows
- Has a non-trivial `downgrade()` that raises `NotImplementedError("embedding-v2 initial migration is not reversible; restore from backup.")`.

On a **fresh install**: `alembic upgrade head` runs this one migration and the DB is at the right state. The sentinel rows are populated. The app boots cleanly.

On an **existing install** (with `alembic_version = '0022'` or similar): `alembic upgrade head` fails with `Can't locate revision identified by '0022'` because that revision no longer exists in the script tree. The error is loud and the app refuses to start. The operator must either (a) wipe + re-launch, or (b) run the external migration script (Component 7).

**Optional polish in MigrationRunner.swift:** catch this specific alembic error and surface a friendlier menubar message: "Schema mismatch — see upgrade runbook (docs/upgrade-runbook.md#embedding-v2)". Low priority; the raw alembic error is also actionable.

### 7. External migration script — `scripts/migrate_to_embedding_v2.py`

**Standalone Python**, outside the alembic flow, for operators who want to migrate an existing DB rather than wipe + re-ingest.

**Invocation:**
```bash
uv run python scripts/migrate_to_embedding_v2.py \
  --db-url postgresql://harbor_clerk@localhost/harbor_clerk \
  --confirm
```

**Pre-flight refuses to run if:**
- `--confirm` flag missing.
- `schema_metadata` table already exists and `embed_model` row is present (DB is already migrated).
- `alembic_version.version_num` does NOT equal the expected pre-rebase head (`'0022'` at time of writing). The expected head is hardcoded in the script — if the prod DB is at a different head, the script refuses rather than guess.
- The current `chunks.embedding` column is not present, or is not `vector(384)`.

**Operations** (single transaction where possible):
1. `CREATE TABLE schema_metadata` with the three sentinel rows.
2. `ALTER TABLE chunks DROP COLUMN embedding`.
3. `ALTER TABLE chunks ADD COLUMN embedding vector(768)`.
4. `DROP INDEX chunks_embedding_hnsw_idx`.
5. `CREATE INDEX chunks_embedding_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)`.
6. `UPDATE alembic_version SET version_num = '<revision id of the rebased 0001_initial.py>'`. The actual revision id is the value alembic generates at the top of the rebased migration file; the script reads it from a module-level constant that matches.
7. Enqueue `embed` stage for every doc with `pipeline_status='ready'`, ordered `documents.updated_at DESC` (most-recent-first). The script only enqueues; the actual re-embed runs when workers come back up on the new binary.

**Logging:**
- stdout: progress per step
- Also writes a log file under the standard log dir (`~/Library/Application Support/Harbor Clerk/logs/migrate_to_embedding_v2.log` on macOS, `./logs/migrate_to_embedding_v2.log` otherwise) so the operation has a permanent record.

**Failure mode:** if any step raises, the transaction rolls back, the script exits non-zero, the DB is unchanged. No partial-migration state.

`--help` documents the operations in plain English and links to the upgrade runbook.

## Data Flow

### Boot
1. API process starts.
2. Alembic check (already in lifespan): warns on version mismatch.
3. **NEW**: `verify_schema_sentinel()` — panics out if `schema_metadata` doesn't match settings.
4. Lifespan completes, FastAPI begins serving.

Same flow for workers: check sentinel before pulling the first job.

### Ingest (`embed` worker stage)
1. Worker pulls an `embed` job from the queue.
2. Loads chunks where `embedding IS NULL`.
3. Batches into groups of 64; POSTs to embedder `/embed` with `task="passage"` (param ignored by Granite-R2 but kept for backward compat).
4. Writes the returned 768-dim vectors into `chunks.embedding`.
5. Re-check `pipeline_seq` between batches (existing race-prevention logic).
6. Mark stage done.

**Stage timeout** raised from 1800s → 3600s. Granite-R2 is ~5× slower per chunk than e5-small on Mac mini silicon; large docs (~500 chunks) take ~25s with e5-small vs ~2 min with Granite. The doubled timeout keeps headroom.

### Search (`POST /api/search` or `kb_search` MCP tool)
1. Receive query.
2. `hybrid_search()` runs FTS (en + fr) + vector cosine; merges scores; produces top `min(K + reranker_top_k_pad, reranker_pool_size)` candidates. With defaults (`K=10`, `pad=40`, `pool_size=50`): 50 candidates flow to the reranker. A caller asking for `K=20` sees the same 50 (capped by `pool_size`); `K=100` would yield `min(140, 50) = 50`.
3. If `settings.reranker_enabled`:
   - Format candidates as `"Title: {doc_title}\n\nChunk: {chunk_text}"`.
   - POST to reranker `/rerank` with `top_k=K`.
   - Reorder candidates by returned scores; set `hit.score = reranker_score`.
   - On failure: if `reranker_strict`, raise 503; else log warning and continue with hybrid-only top-K.
4. Populate `score_breakdown` on every returned hit (for future inspector).
5. Return top-K.

## Settings (additions to `src/harbor_clerk/config.py`)

```python
embed_model: str = "ibm-granite/granite-embedding-311m-multilingual-r2"
embed_dim: int = 768
embed_needs_prefix: bool = False  # Granite uses CLS pooling; e5 needed query:/passage:

reranker_enabled: bool = True
reranker_url: str = "http://reranker:8001"  # docker default; macOS overrides via env
reranker_top_k_pad: int = 40                # how many extra hits to fetch above K for reranking
reranker_pool_size: int = 50                # hard upper bound on passages sent per /rerank call
reranker_strict: bool = False               # raise vs fall back on reranker outage
reranker_timeout_seconds: float = 30.0      # per-call HTTP timeout
```

All read from env vars (pydantic-settings); defaults work for the standard Docker layout. macOS overrides `reranker_url` via the existing env-var-from-config pattern.

## API Surface

**No tool signatures change.** Both `POST /api/search` and the `kb_search` MCP tool keep their existing request schemas (no new required params).

**Response additions** to `SearchHitOut` (`src/harbor_clerk/api/schemas/search.py`):

```python
class SearchHitOut(BaseModel):
    # ... existing fields ...
    score: float                              # NOW: reranker score when reranking enabled
    score_breakdown: ScoreBreakdown | None    # NEW: details for the Phase-5 inspector

class ScoreBreakdown(BaseModel):
    fts: float
    vector: float
    hybrid: float
    reranker: float | None
```

**Response addition** to `SearchResponse`:

```python
class SearchResponse(BaseModel):
    # ... existing fields ...
    reranker_status: Literal["ok", "disabled", "failed"]  # NEW
```

`reranker_status` lets the frontend show a banner when fallback is active. Internal value, not exposed via MCP tool descriptions.

## Error Handling

| Failure mode | Handling |
|---|---|
| Sentinel mismatch at boot | `CRITICAL` log + `sys.exit(2)`. No auto-migration. |
| `schema_metadata` table missing at boot | Same as sentinel mismatch; message explicitly names the missing table. |
| Reranker HTTP timeout | `reranker_strict=False` (default): log WARNING, set `reranker_status="failed"`, return top-K from hybrid pool unchanged. `reranker_strict=True`: raise HTTPException 503 to the caller. |
| Reranker returns malformed JSON | Same as timeout. |
| Embedder timeout during ingest | Existing `embed` stage retry logic; stage marked error after the existing timeout. |
| External migration script: pre-flight check fails | Print which check failed + remediation; exit non-zero. DB untouched. |
| External migration script: mid-operation exception | Transaction rolls back; exit non-zero; log captured. DB consistent (no partial migration). |
| Granite-R2 model file corrupt at startup | Embedder process refuses to start; ServiceManager on macOS / docker healthcheck flags it; HC API will surface this via `/api/system/health`. |

## Testing Strategy

| Test file | What it covers | Real-or-mocked deps |
|---|---|---|
| `tests/test_db_health.py` | `verify_schema_sentinel()` happy path + mismatch panic | Real Postgres test DB; populates `schema_metadata` directly |
| `tests/test_search_rerank.py` | `rerank_hits()`: ordering, top_k truncation, fallback on HTTP failure, strict mode | `httpx_mock`-style stub reranker |
| `tests/test_search_rerank_integration.py` | End-to-end `hybrid_search()` with real reranker | Marked `requires_models` (new pytest marker, registered in `pyproject.toml` alongside the existing `integration` marker); not run in CI by default; runs locally + nightly |
| `tests/test_embedder_granite.py` | Granite-R2 loads, returns 768-dim, handles batch=64 | Same `requires_models` marker |
| `tests/test_migrate_to_embedding_v2.py` | External migration script: pre-flight refusals + happy-path on a fixture old-schema DB | Real Postgres test DB with a fixture loaded from a v0.X SQL dump |
| `tests/test_reranker_service.py` | Reranker FastAPI app: empty passages, top_k > len, malformed input | In-process test client |
| `tests/test_alembic_initial.py` | Rebased `0001_initial.py` runs cleanly against an empty DB and yields the expected `schema_metadata` + `chunks.embedding` type | Real Postgres test DB |

**Retrieval-eval gate** (NOT in CI):
```bash
sweep --mode retrieval-eval \
  --run-id 2026-05-05-prod \
  --api-base http://localhost:8100 \
  --label embedding-v2
```
Pass criteria, applied as the final pre-merge gate:
- Overall recall@10 improvement: **≥ +0.08**
- No per-corpus regression worse than **-0.05**
- nDCG@10 also improves overall
- CSV + summary committed in the PR description as a code block

The retrieval-eval gate is a **manual checkbox** in the PR template, run by the human reviewer (or by the author before requesting review). NOT enforced by CI because it requires a running HC instance with the prod-scale corpus.

## Rollout Plan

For developers iterating on the branch:
1. Work in `feat/embedding-v2-granite-and-reranker` worktree.
2. Unit tests + format/lint run as normal in CI.
3. Local integration tests run with `requires_models` marker enabled.
4. **Do NOT rebuild the user's HarborClerkServer.app from this branch** until ready to commit to the upgrade — the rebuilt app's MigrationRunner will fail to start against the user's existing `0022`-version DB.

For operators upgrading an existing deployment:
1. **Path A (recommended for office appliances):** Stop HC. Drop the database. Re-launch from the new binary. Watched folders auto-rescan and re-ingest with Granite-R2 embeddings. Search works as soon as the first few docs finish ingesting; full corpus rebuild trickles in the background.
2. **Path B (preserve existing DB):** **Stop HC** (workers + API + embedder all down). Run `scripts/migrate_to_embedding_v2.py --db-url <...> --confirm`. Wait for the script to finish (a few seconds — schema change + enqueue, no re-embed). **Re-launch HC from the embedding-v2 binary.** Sentinel check passes (script populated it). Workers come up against Granite-R2 embedder, see the queued `embed` jobs, and trickle through them most-recent-first. Search works throughout (chunks without new embeddings just don't surface via the vector leg until they're processed).

The "stop HC entirely before running the script" sequencing is important — the script and a running worker should not race on the `chunks.embedding` column type change. The script itself is atomic (single transaction), but a worker mid-batch when the column type changes will fail mid-write.

For the operator running the `2026-05-05-prod` sweep right now: nothing changes during Phase 0 development. The sweep continues against the current HC instance. When the sweep finishes and the operator is ready to evaluate Phase 0, they cut over per Path A or B above, then run the retrieval-eval gate.

## Risks + Mitigations

| Risk | Mitigation |
|---|---|
| Granite-R2 was released April 2026 (~3 weeks before this design); only ~18K downloads/month. Real-world bugs may surface. | Smoke-test the model in `tests/test_embedder_granite.py` with the corpus's actual content. Keep `EMBED_MODEL` env-var-configurable so a rollback to e5-small is a one-line change if Granite has a showstopper. Note: rollback also requires re-running the migration in reverse, which means restoring from backup — flag this in the upgrade runbook. |
| Reranker is a new service; adds health-check surface, restart coordination on macOS. The "Quit hang" pattern in [`project_menubar_process_management_audit.md`](../../../memory/project_menubar_process_management_audit.md) might bite again. | Tier A (PR #341), Tier B (PR #343-345), and Tier C launchd migration (PR #346) all shipped 2026-05-12 — process-group handling, shutdown-aware HealthChecker, Force Stop All menu, launchd for Postgres+Tika are in place. Reranker follows the same `EmbedderService.swift` lifecycle pattern and inherits those fixes. No new process-management work required. |
| Re-embed of a large prod corpus takes hours single-threaded. | Background trickle model; UI badge ("re-embedding: N of M docs"); document the timeline in the upgrade runbook. Acceptable degradation: vector leg has reduced recall during re-embed, but FTS leg is unaffected. |
| Bundling models adds ~2 GB to Docker image + macOS bundle. | Acceptable per project philosophy. Reranker model alone (1.2 GB) is the bigger contributor. Document the size bump in release notes. |
| The `0001_initial.py` rebased migration is a huge file (recreates entire schema). PR review will see a lot of new lines. | Generate it programmatically (`alembic revision --autogenerate` against a fresh DB at current head, then add the embedding-v2 changes manually). Note in the PR description that line count is mechanical. |
| External migration script's hardcoded `'0022'` expected-head will go stale if any migration lands between this design and the Phase 0 implementation. | Re-validate the expected head immediately before opening the PR; if drift has occurred, regenerate the rebased initial migration to include the new changes. |
| Reranker timeout default (30s) might be too short for a Mac mini under heavy load. | Configurable via `reranker_timeout_seconds`. The retrieval-eval gate will surface timeout issues in the metrics CSV (high latency rows). |

## Open Questions Resolved (vs the master plan's Open Questions for Phase 0)

- ~~Reranker deployment: separate service or co-hosted in embedder container?~~ **Separate service.** Different scaling profile, cleaner failure isolation.
- ~~Reranker pool size?~~ **50, configurable.**
- ~~macOS service-management strategy?~~ **Python subprocess via ServiceManager, same pattern as embedder. Not launchd.**
- ~~Granite-R2 download: bake into image or download on first run?~~ **Bake into Docker image AND macOS bundle.**
- ~~Re-embed strategy: blocking maintenance window vs background trickle?~~ **Background trickle, most-recent-first.**

Decisions added in this design beyond the master plan's question list:
- Reranker precision: **fp16**.
- Reranker input format: **`Title: ...\n\nChunk: ...`** (heading chain comes in next phase).
- Reranker failure handling: **fallback to hybrid-only top-K**, configurable strict mode.
- `task` param backward compat: **accepted but ignored** (don't break the existing `embed` worker stage).
- Settings shape: **flat env vars, no provider abstraction** (YAGNI).
- Schema sentinel rows: **drop `upgrade_phase`** — alembic_version is authoritative for schema version.
- `score_breakdown`: **populated unconditionally**, even though no client consumes it yet (wired for Phase 5).
- HNSW params: **unchanged** (`m=16, ef_construction=64`) — index tuning is a separate exercise.
- Worker timeout: **1800s → 3600s** for `embed` stage.
- CI: **unit tests mock the embedder/reranker HTTP**; integration tests gated on `requires_models` marker; retrieval-eval is a manual pre-merge step.
- Alembic strategy: **rebased to `0001_initial.py`**; old migrations deleted; no in-app upgrade migration.
- External migration script: **`scripts/migrate_to_embedding_v2.py`**, refuses to run unless `--confirm` AND `alembic_version = '0022'` AND no existing sentinel.

## Cross-References

- Master plan staging doc: [`docs/superpowers/plans/2026-05-17-retrieval-mcp-upgrade-master-plan.md`](../plans/2026-05-17-retrieval-mcp-upgrade-master-plan.md)
- Retrieval-eval harness (already shipped): [`scripts/test_corpora/runner/retrieval_eval.py`](../../../scripts/test_corpora/runner/retrieval_eval.py)
- Sweep baseline source: [`memory/project_sweep_2026_05_05_prod_resume.md`](../../../memory/project_sweep_2026_05_05_prod_resume.md)
- Macos process-management context: [`memory/project_menubar_process_management_audit.md`](../../../memory/project_menubar_process_management_audit.md)
