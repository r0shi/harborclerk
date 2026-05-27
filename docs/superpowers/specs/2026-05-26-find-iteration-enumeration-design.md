# Find-iteration: unified enumeration tool

**Status:** spec
**Date:** 2026-05-26
**Predecessors:** PR-D (#396, kb_search descriptions), PR-E (#398, `kb_documents_by_date` + `kb_verify_identifier`), PR-J (#409, MCP docstring tuning), `pr_followups.md` PR #385 entry (find-iteration gap analysis), `project_mcp_docstring_lever_weak.md` (docstring is a weak lever).

---

## Background

The 2026-05-05-prod sweep and follow-up captures surfaced a coverage gap on "find all" / "list every" / enumeration questions. Closer inspection of three Enron find captures showed the gap is not a single failure mode but three:

| qid | tool calls | queries | offset | cited | truth | shape |
|---|---|---|---|---|---|---|
| `enron-find-offbalancesheet` | 2 | 1 | 0 | 13 | 17 | **One-shot stop** — 1 search call, stopped despite hits |
| `enron-find-ferc` | 9 | 7 varied | 0 | 40 | 50 | **Coverage cap on relevance** — iterated well, long tail still missed |
| `enron-find-layforwarded2001` | 12 | 11 varied | 2 | 71 | 126 | **Structured-filter shape** — gold = "from Lay, in 2001"; semantic search can't enumerate 126 same-content emails |

Per-model coverage is consistent: both Sonnet 4.6 and gpt-4o show the same pattern (PR #387). PR-J's MCP docstring nudge ("ONE page is not the answer — drain via offset") was shipped but its BEFORE/AFTER experiment couldn't measure it (the `finds_short` population in `pr_j_populations.json` was empty). The lesson from PR-J's null result was that docstring-level guidance is a weak lever for behavioral change — the production fix needs a tool affordance, not just stronger prose.

The shapes have different root causes but a shared affordance gap: there is no tool today that says "give me ALL matches up to a cap" with a clear stop condition and a literal-substring filter. `kb_search` is a top-K relevance tool — the model has to construct the enumeration loop itself, which both Sonnet and gpt-4o get wrong in different ways.

This spec adds a unified enumeration tool that handles shapes 1 and 2 directly. Shape 3 (FERC-style coverage on pure relevance) is a closer judgement call — the model iterated well, cited 40 of 50. We treat it as adequate and don't try to lift the relevance ranker.

## Goal

A new MCP tool `kb_find_all` (plus its `_BASE_CHAT_TOOLS` mirror `find_all_documents`) that:

1. Returns up to `max_results` *documents* (deduped by `doc_id`), not chunks.
2. Iterates server-side until the result set is exhausted or `max_results` is reached. The model gets the answer in one call.
3. Accepts a `text_contains` literal-substring filter so "find emails containing 'off-balance-sheet'" can match the gold criterion directly rather than relying on relevance ranking of the phrase.
4. Lets the model pick sort order (`relevance` | `date_desc` | `date_asc`).
5. Supports `offset` for pagination past the cap.
6. Has a per-model default `max_results` plumbed through `ModelInfo` so HC's local-LLM paths (chat, research) can tune the cap without changing the tool surface.

## Non-goals

- **Lifting the relevance ranker for shape 3** (FERC). That's a retrieval-quality question, separate from enumeration. Out of scope.
- **Changing `kb_search` behavior.** It stays a top-K relevance tool. Adding `text_contains` as a shared filter on `kb_search` *as well* is in scope (small win, no behavior change for existing callers); changing its response shape is not.
- **Aggregation tools** (`kb_count_matches`, group-by, etc.). Already deferred in `pr_followups.md` under PR-E; not pulled in here.
- **Streaming / chunked responses.** Server collects the full payload, returns once. Token economy is managed via `presentation="brief"` and `max_results`.
- **Local-model per-model experimentation as a deliverable.** The override surface ships; the per-model tuning is its own follow-up work (mentioned in §7).

## §1 — Tool surface

### `kb_find_all` (MCP)

Added to `src/harbor_clerk/mcp_server.py` alongside the existing `kb_*` tools.

```python
async def kb_find_all(
    query: str,
    *,
    text_contains: str | None = None,
    max_results: int = 100,
    offset: int = 0,
    presentation: Literal["brief", "full"] = "brief",
    sort_by: Literal["relevance", "date_desc", "date_asc"] = "relevance",
    # Shared filters with kb_search:
    after: str | None = None,
    before: str | None = None,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    metadata_filter: dict[str, Any] | None = None,
) -> dict:
    ...
```

**Response shape:**

```python
{
    "results": [                         # up to max_results, deduped by doc_id
        {
            "doc_id": "uuid",
            "doc_title": "...",
            "mime_type": "application/pdf",
            "language": "english",
            "score": 0.83,               # max chunk score for this doc (under chosen sort)
            "ingested_at": "2026-...",   # iso8601
            "page_range": "1-12",        # if multi-page
            # presentation="full" adds:
            "top_chunk": {               # the best chunk for this doc, ~500 chars
                "chunk_id": "uuid",
                "text": "...",
                "page": 4,
                "heading": "...",
            },
        },
        ...
    ],
    "total_matches": 287,                # total docs matching (post-dedupe, post-filter)
    "returned": 100,                     # len(results)
    "offset": 0,                         # echoed back
    "truncated": true,                   # total_matches > offset + returned
    "sort_by": "relevance",              # echoed back
    "presentation": "brief",             # echoed back
}
```

### `find_all_documents` (local chat tool)

Added to `_BASE_CHAT_TOOLS` in `src/harbor_clerk/llm/tools.py` alongside the other simplified chat tools. Same parameters, same response shape, simpler description aimed at 4-8B local models.

The MCP tool dispatches to a shared implementation; the chat-tool executor calls the same backing function. One source of truth.

## §2 — Semantics

**Dedupe by `doc_id`.** A document with 8 chunks matching the query produces ONE row in `results`. `score` is the max chunk score per doc under the current sort.

**`total_matches` is post-dedupe, post-filter, scope-aware.** For API keys with `scope_folder_ids`, the count reflects the scoped corpus only. For an unscoped key, the full corpus count.

**`max_results` is clamped** server-side: `max(1, min(max_results, settings.find_all_max_results_cap))`. Default cap: 500. Hard ceiling for safety against runaway responses.

**`offset` is pre-truncation.** If `total_matches=287` and the model asks `offset=100, max_results=100`, it gets docs 100-199 of the 287-doc sorted set. `truncated` is `true` while `offset + returned < total_matches`.

**`sort_by` semantics:**
- `"relevance"` (default): order by max chunk score per doc, descending. Date as tie-breaker (most recent first).
- `"date_desc"`: order by `documents.ingested_at` (or `created_at` for non-watched docs), most recent first.
- `"date_asc"`: same field, oldest first.

**`presentation="brief"` payload budget.** Brief rows are ~120-180 chars JSON each; 100 rows ≈ 15 KB. `presentation="full"` adds a ~500-char top chunk per row, putting 100 rows at ~65 KB. The tool docstring caps `max_results` at 30 for `full` presentation (server-side clamp) to keep the payload bounded.

## §3 — `text_contains` filter

A new literal-substring filter, added to **both** `kb_find_all` AND `kb_search` (cheap win on `kb_search`; same SQL clause).

**Semantics:** case-insensitive substring match against `chunks.chunk_text`. A document is a candidate iff *at least one of its chunks* contains the substring. Matches are AND-ed with the relevance query — `kb_find_all(query="off-balance-sheet accounting", text_contains="off-balance-sheet")` returns docs that literally contain the phrase, ranked by relevance to "off-balance-sheet accounting".

**Implementation:** `chunks.chunk_text ILIKE '%' || :text || '%'` with the `pg_trgm` GIN index (HC already has `pg_trgm` enabled; existing index on `chunks.chunk_text` if present, else added in this PR). For short substrings (<3 chars), the trgm index is ineffective and the planner falls back to seq scan — we'll log a warning and the docstring will recommend longer substrings.

**Escaping:** routed through the shared `escape_ilike()` helper (`src/harbor_clerk/sql_escape.py`, added in PR-H) — `%` and `_` in the user's substring are escaped to literal characters. PR-H's audit established this helper for all ILIKE call sites; new ones must use it.

**Use-case framing in docstring:** "If the question names a literal phrase that must appear ('emails containing X', 'contracts that mention Y'), pass `text_contains=X` so semantic ranking doesn't dilute exact matches."

## §4 — Per-model `max_results` override surface

Per the discussion: the MCP server enforces the hard cap and the default of 100; HC's *local* chat/research paths can plumb a per-model default at call construction time.

Three layers, lowest precedence first:

1. **Hard cap** (`settings.find_all_max_results_cap`, default 500) — server clamps any request above this.
2. **Tool default** (`max_results=100` in the MCP signature) — what the model gets if it doesn't pass the argument.
3. **Per-model override at the chat-path call site** — in `src/harbor_clerk/llm/chat.py` and `llm/research.py`, when constructing tool calls for a local model, pass `max_results=ModelInfo.find_all_default_max_results or 100`.

**New `ModelInfo` field:** `find_all_default_max_results: int | None = None` in `src/harbor_clerk/llm/models.py`. `None` means "use the tool default of 100". The 8 curated models in `models.py` all start with `None`; the field is the experimentation surface for future per-model tuning (e.g. SmolLM 3B might want 30, GPT-OSS 20B might want 150).

**Cloud-MCP path** (Claude.ai connector, eval harness using AnthropicProvider): the model itself picks `max_results`. The MCP server enforces the cap. No per-model override on this path — cloud frontier models are smart enough to ask for what they need.

## §5 — Implementation sketch

Not file-level — that's the plan's job. Shape only.

**One new function** in `src/harbor_clerk/search.py`: `async def find_all(...) -> FindAllResult` mirroring the shape of `hybrid_search` but doing doc-level aggregation.

- Reuses the existing FTS+vector candidate generation from `hybrid_search` (call it with a larger internal k — say `min(max_results * 5, 1000)` — to get the candidate pool).
- Applies the `text_contains` filter at the chunk level (SQL clause on `chunks.chunk_text`).
- Aggregates chunks → docs: GROUP BY `doc_id`, MAX(normalized_score) → per-doc score.
- Applies the chosen sort.
- Returns total count + sliced `results[offset : offset + max_results]`.

**Reranker handling:** the doc-level scores fed to the rerank stage are the per-doc MAX chunk scores. Reranker stays optional (existing `RERANKER_ENABLED` setting). If on, it reranks the top-`reranker_pool_size` docs by cross-encoder score before the final slice.

**Scope filter:** API key's `scope_folder_ids` / `scope_topic_ids` are applied at the chunk level (same as `hybrid_search`) before aggregation.

**Backing for the chat-tool executor:** `src/harbor_clerk/llm/tools.py` already has a per-tool executor pattern. The `find_all_documents` executor calls the same `search.find_all(...)` function with a thinner argument map.

## §6 — Testing / eval

Three layers:

**Unit tests** (`tests/test_find_all.py`):
- Dedupe correctness: doc with N chunks shows up once.
- Sort by all three modes; tie-breaker behaviour.
- `offset` pagination is stable across calls.
- `text_contains` matches case-insensitively, escapes special chars.
- `max_results` clamp at the hard cap.
- Scope filter respected.
- `total_matches` is post-filter, post-dedupe.

**Tool description tests** (`tests/test_mcp_tool_descriptions.py`):
- `kb_find_all` in the tool list.
- Docstring contains the "find all / enumerate / list every" trigger phrase.
- Docstring distinguishes from `kb_search` ("use kb_search for top-K, kb_find_all for all-matches").

**Eval impact measurement.** The three find items (`enron-find-offbalancesheet`, `enron-find-ferc`, `enron-find-layforwarded2001`) and any new corpus finds become the post-merge BEFORE/AFTER target:
- BEFORE: today's main (no `kb_find_all`) — already captured in 2026-05-05-prod.
- AFTER: with `kb_find_all` shipped, re-run those three items + the synthetic finds.
- Compare cited count vs truth count; expect lift on offbalancesheet (Shape 1, one-shot stop) and layforwarded (Shape 2, structured filter via `text_contains` or `metadata_filter`). FERC (Shape 3, coverage cap) is a null hypothesis.
- Eval done via the existing `scripts/test_corpora` harness with a `--refresh` on the three qids.

## §7 — Out of scope / follow-ups

- **Per-model `find_all_default_max_results` tuning.** Field ships defaulting to `None` (= 100). Experiment data will populate it later. Track in memory once we have local-model captures.
- **Aggregation / count-only variants** (`kb_count_matches`). Still deferred from PR-E. If real usage shows the model wants pure counts without the result list, revisit.
- **Sort by other fields** (title, mime_type, custom metadata). Three sort options is enough for v1.
- **Token-budget-based cap** (instead of doc count). Not now — predictable doc count is easier to teach.
- **Streaming responses.** If `max_results=500` payloads start hurting latency, revisit with chunked SSE.
- **Reranker tuning specific to enumeration.** Reranker is on the per-doc score; if eval shows the cross-encoder underperforms on enumeration (because it was trained for top-K relevance), a `rerank=False` flag on `kb_find_all` may be needed.

## §8 — Open questions

None blocking the implementation plan. The two minor ones, settled inline:
- **`text_contains` on `kb_search` too?** Yes — same SQL clause, small win on shape-1 questions even when the model doesn't reach for `kb_find_all`. Tested separately.
- **Should `kb_find_all` skip the reranker by default?** No. Reranker stays on (per setting); it's still useful for ranking the top docs even when enumerating. If eval shows it slows things down, the `rerank=False` follow-up handles it.
