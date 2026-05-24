# PR-D — MCP tool description rewrites + `kb_search` `discriminator_hint`

**Status:** spec — awaiting user review before plan + implementation
**Author:** Claude (with Alex)
**Companion docs:** PR-F spec (`2026-05-24-document-metadata-extractors-design.md`) — PR-D sits on top of PR-F's `metadata_filter`. PR-A/B/C answer-eval specs for the failure-mode motivation.

## Goal

Improve recall/utility of HC's MCP tools for frontier cloud models by (1) rewriting all 16 `kb_*` tool descriptions from API docs into behavior guides, and (2) adding a `discriminator_hint` field to `kb_search` responses that flags when top-K results share text but differ on a structured metadata field — pointing models toward `metadata_filter` for disambiguation. Both changes are HC-side and apply to every MCP caller, not just our eval harness.

## Why this PR exists (cross-corpus + cross-provider failure modes)

The PR-A/B/C eval runs surfaced three product signals confirmed cross-corpus AND cross-provider:

1. **Negative-hedging** — both Sonnet and gpt-4o fabricate plausible-looking answers when the question's identifier doesn't exist in the corpus, rather than declining. PR-C's gpt-4o run: `synth-neg-invoice-99999` → model invented a `$39,974.87` total from an unrelated invoice (correctness 0).
2. **Boundary-doc retrieval** — both providers trust HC's top-1 retrieval and answer from the wrong doc when the question's identifier is ambiguous (Pinnacle contract X vs Y; "Senior Project Coordinator" onboarding letter; "Marbledock Elevate 2025" campaign brief). HC's retrieval picks a neighbouring doc; the model commits without disambiguating.
3. **Find under-iteration** — both providers stop after 1-2 `kb_search` calls even when the corpus has 17, 50, or 126 matching docs. Coverage scores collapse on large-truth find items.

PR-F (#389) shipped the data primitives for fix #2: `Document.metadata` + `kb_search`'s `metadata_filter` param. But the eval data shows models don't reach for new tools spontaneously — they follow the *behavior* their tool descriptions describe. The current 16 kb_* descriptions read like API docs ("Search the knowledge base"); they don't tell models *when* to iterate, *when* to decline, *how* to use the disambiguation primitives we now have. PR-D closes that loop.

`discriminator_hint` is the active complement: when HC detects that top-K results are ambiguous on a structured field, it surfaces a concrete pointer to `metadata_filter` in the response. The model doesn't have to spot the ambiguity itself.

## Non-goals

- **No new MCP tools.** `kb_verify_identifier` and `kb_documents_by_date` are queued for PR-E. PR-D works strictly with the existing 16 + the kb_search response shape.
- **No prompt-tuning experiment.** The eval-harness `DEFAULT_SYSTEM_PROMPT` in `scripts/test_corpora/runner/providers/base.py` is a separate lever (queued as a future experiment). PR-D only touches HC-side tool descriptions, which apply to all callers including production users.
- **No `total_matching_estimate` / `more_available` additions.** `kb_search` already returns `total_candidates` and `has_more` (verified at `mcp_server.py:532-537`). PR-D just teaches the descriptions to mention them.
- **No structured pagination changes.** Existing `offset` + `has_more` pagination semantics stay.
- **No metadata-filter operators (`>=`, `<`, `in`).** Still v1 exact-match-only from PR-F. Operator support is its own future PR.
- **No retrieval-algorithm changes.** PR-D is pure surface-level (descriptions + a derived response field). The underlying FTS+vector hybrid search is unchanged.
- **No new MCP server module structure.** Edits stay scoped to `mcp_server.py` and the existing `search.py` / `search_types.py`.
- **No backwards-compat shim.** `discriminator_hint` is additive — existing callers that ignore unknown response fields are unaffected.

## Background — what's in HC today

**16 kb_* tools** registered in `src/harbor_clerk/mcp_server.py` via `@mcp.tool()` decorators. Current descriptions are docstrings on the async functions; they vary in quality but generally read as "describe what the function returns" rather than "describe how the model should use it."

**`kb_search` response shape** (existing, partial, lines 533-538 of `mcp_server.py`):
```python
{
    "hits": [...],                # list of SearchHit dicts
    "total_candidates": int,       # how many chunks matched before pagination
    "has_more": bool,              # offset + k < total_candidates
}
```

Plus optional fields: `possible_conflict`, `conflict_sources` (already exists when top hits diverge); `would_match_unscoped` (for scoped principals); `metadata_filter` echo would be useful but isn't currently surfaced.

**`Document.metadata`** (shipped in PR-F) — JSONB column populated at ingest by sidecar / Tika / frontmatter extractors. Indexed by GIN for `@>` containment.

**`metadata_filter`** (shipped in PR-F) — `kb_search` parameter that filters by `{namespace.key: value}` pairs via JSONB containment + existence fallback.

**`SearchHit`** at `src/harbor_clerk/search_types.py` — already includes `doc_id`, `doc_title`, `score`, `text`, `pages`, `chunk_id`. The hit has everything needed to compute the discriminator_hint downstream.

## Architecture

Two distinct sub-features, in one PR:

```
src/harbor_clerk/
├── mcp_server.py
│   ├── (all 16 kb_* docstrings rewritten — see "Tool description rewrites" below)
│   ├── kb_search() body
│   │   └── + call to _compute_discriminator_hint(...) before json.dumps
│   └── (existing logic unchanged otherwise)
└── search.py (or new mcp_helpers.py)
    └── _compute_discriminator_hint(hits, session) — pure post-processing
tests/
├── test_mcp_tool_descriptions.py     (NEW)  — grep-style assertions
└── test_mcp_discriminator_hint.py     (NEW)  — trigger + edge cases
```

The discriminator_hint computation is a pure function over `(hits, session)`. It runs after `hybrid_search` returns, fetches the metadata for the candidate docs from the database (single SELECT against `documents.metadata`), and returns a dict if a hint applies or `None` if it doesn't. The `kb_search` body just inserts the dict into the response when non-`None`.

The hint code goes into a new private helper module — call it `src/harbor_clerk/mcp_discriminator.py` to keep `mcp_server.py` from growing further. Single-responsibility file, ~80 lines including comments.

## Tool description rewrites

Every kb_* tool docstring gets reshaped to a consistent 4-part structure:

1. **What it does** (one sentence, mostly unchanged from current)
2. **When to use it** (explicit behavior nudge — *"use after kb_search returns ambiguous results"* / *"use to verify before answering"* / etc.)
3. **What to do with the output** (what fields matter and what they imply — `has_more` → keep iterating; `discriminator_hint` present → use `metadata_filter`; `metadata` field on `kb_get_document` → inspect available filter keys before crafting a filter)
4. **How to fail / when to stop** (explicit decline guidance — *"if the question's identifier doesn't match any retrieved doc, say so plainly rather than reporting the closest match"* — the negative-hedging fix)

Effort tiers (consistent 4-part treatment across all 16, but rewrite depth differs):

### Major (4 tools — negative-hedging + iteration nudges)

- **`kb_search`** — the workhorse. Add: (a) explicit decline guidance for negative answers, (b) reach-for-`kb_batch_search` nudge when one query is ambiguous, (c) `metadata_filter` example with `kb_get_document`-as-discovery-mechanism, (d) `discriminator_hint` interpretation guidance, (e) `has_more` → paginate or refine guidance.
- **`kb_batch_search`** — currently under-used by gpt-4o (Sonnet reaches for it; gpt-4o doesn't). Add: *"prefer this over multiple sequential kb_search calls when you need to triangulate"*, scoring guidance ("docs that appear in multiple batch queries are strongly corroborated"), and the same decline guidance as `kb_search`.
- **`kb_read_passages`** — for verifying answers. Add: *"call this on top kb_search hits before committing to an answer that depends on specific chunk text"*; the verify-before-claim pattern that the negative-hedging items needed.
- **`kb_get_document`** — for full-doc inspection. Add: *"the `metadata` field shows what `metadata_filter` keys are available for this doc and its corpus"*; the discovery loop that completes the metadata-filter feedback.

### Moderate (4 tools — cross-references + behavior nudges)

- **`kb_expand_context`** — *"use after kb_read_passages when the chunk text alone doesn't show enough surrounding context"*.
- **`kb_find_related`** — *"use to find related docs by overlap; complement to kb_search when you have one good hit and want to expand the relevance set"*.
- **`kb_document_outline`** — *"use to navigate inside a document by section; pair with kb_read_passages to read specific sections"*.
- **`kb_corpus_overview`** — *"use FIRST when you don't know the corpus shape — what doc types exist, what dates are covered, what topics dominate"*.

### Light (5 tools — clarity polish, minimal behavior change)

- **`kb_entity_search`**, **`kb_entity_overview`**, **`kb_entity_cooccurrence`** — for entity-driven queries; clarify when entity search beats free-text search (entity disambiguation, "documents mentioning Person X").
- **`kb_list_recent`** — temporal exploration.
- **`kb_read_document`** — full-doc text dump; clarify when it beats `kb_get_document` (full content needed vs metadata + summary).

### Admin/diagnostic (3 tools — no behavior change needed)

- **`kb_ingest_status`**, **`kb_reprocess`**, **`kb_system_health`** — light clarity pass for consistency; these are operator-facing and rarely called by models.

**Description style guide** (informs every rewrite):
- Lead with behavior, not API surface. Wrong: *"Returns chunks sorted by relevance score."* Right: *"Use to find specific facts in the corpus. Top hit is usually the answer; verify with `kb_read_passages` if the chunk text is short or ambiguous."*
- Use second-person imperative ("Use X to...") not third-person passive.
- Concrete examples beat abstract guidance. Wrong: *"Use metadata filters when appropriate."* Right: *"`metadata_filter={'sidecar.vendor': 'Acme'}` to pin the Acme contract among several."*
- Be explicit about decline conditions. Models default to "produce an answer" when uncertain; the description must overcome that.

## `discriminator_hint` computation

### Trigger conditions

Compute `discriminator_hint` ONLY when all of:

1. `len(hits) >= 2` (single-result responses are trivially unambiguous).
2. At least 2 distinct `doc_id`s among the top hits considered (deduping chunks from the same doc).
3. The doc_ids whose top chunk-score is within `max(0.05, 0.1 * top_score)` of the overall top score — these are the "candidate" docs whose ranking is close enough to be ambiguous.
4. After fetching `documents.metadata` for the candidate docs, at least one structured field path (`<namespace>.<key>`) has differing values across them.

If any condition fails, omit `discriminator_hint` entirely from the response (don't include `discriminator_hint: null`).

### Algorithm

```python
def _compute_discriminator_hint(hits: list[SearchHit], session) -> dict | None:
    if len(hits) < 2:
        return None

    # Group hits by doc_id; keep the best score per doc.
    by_doc: dict[str, float] = {}
    titles: dict[str, str] = {}
    for h in hits:
        if h.doc_id not in by_doc or h.score > by_doc[h.doc_id]:
            by_doc[h.doc_id] = h.score
            titles[h.doc_id] = h.doc_title or h.doc_id

    if len(by_doc) < 2:
        return None  # all hits from one doc

    # Candidate set: docs within ε of the top score
    top_score = max(by_doc.values())
    epsilon = max(0.05, 0.1 * top_score)
    candidates = [did for did, s in by_doc.items() if s >= top_score - epsilon]

    if len(candidates) < 2:
        return None

    # Fetch metadata for the candidates
    docs_rows = session.execute(
        select(Document.doc_id, Document.doc_metadata)
        .where(Document.doc_id.in_([uuid.UUID(c) for c in candidates]))
    ).all()
    metadata_by_doc = {str(row.doc_id): (row.doc_metadata or {}) for row in docs_rows}

    # Find differing fields across the candidates
    differing = _find_differing_metadata_fields(candidates, metadata_by_doc, titles)
    if not differing:
        return None

    # Pick the top 1-3 most discriminating fields (those with the most unique values)
    top_fields = sorted(differing.items(), key=lambda kv: -len(set(kv[1].values())))[:3]

    suggestion = _build_suggestion(top_fields, titles)
    return {
        "ambiguous_doc_ids": candidates,
        "ambiguous_doc_titles": [titles[d] for d in candidates],
        "differing_metadata": dict(top_fields),
        "suggestion": suggestion,
    }
```

Helper functions:

- **`_find_differing_metadata_fields(candidates, metadata_by_doc, titles)`** — walks the metadata dicts, finds paths `<namespace>.<key>` where at least two candidates have different (non-null) values. Skips `_source_provenance` (internal); skips paths where any candidate is missing the field (can't filter on something that doesn't apply). Returns `{path: {title: value, title: value, ...}}`.
- **`_build_suggestion(top_fields, titles)`** — constructs the human-readable string. Picks the most discriminating field (most unique values), surfaces 1-2 concrete filter values.

### Output shape

When the hint applies:

```jsonc
{
  "hits": [ /* ... unchanged ... */ ],
  "total_candidates": 47,
  "has_more": true,
  "discriminator_hint": {
    "ambiguous_doc_ids": ["uuid-of-0131", "uuid-of-0149"],
    "ambiguous_doc_titles": ["0131_vendor_contract", "0149_vendor_contract"],
    "differing_metadata": {
      "sidecar.term_months": {
        "0131_vendor_contract": 24,
        "0149_vendor_contract": 12
      },
      "sidecar.monthly_fee_usd": {
        "0131_vendor_contract": 8500.0,
        "0149_vendor_contract": 12000.0
      }
    },
    "suggestion": "Top results are ambiguous. Use metadata_filter={'sidecar.term_months': 24} or {'sidecar.monthly_fee_usd': 8500.0} to pin one doc."
  }
}
```

When the hint doesn't apply: the field is absent. Existing callers ignoring unknown fields see no change.

### Edge cases

- **All candidate docs have empty `doc_metadata`** → no differing fields, hint absent. Common for corpora without sidecars/Tika output; degrades gracefully.
- **Candidate docs share all metadata field values** → no differing fields, hint absent. Common when the discriminator is in the doc TEXT, not the metadata (PR-D doesn't try to discriminate by text — that's an embedding-derived signal that would need its own design).
- **One candidate has a field that others don't** → that field is skipped from "differing" (can't filter on it without excluding the others). Could be relaxed in v2 but the v1 spec is "fields ALL candidates have, with at least 2 distinct values."
- **`metadata_filter` was used in the calling query** → the candidates already match the filter; still useful to surface remaining ambiguity on OTHER fields. The hint logic doesn't special-case this — it operates on the post-filter result set.

### Cost

Cheap. One additional SELECT per kb_search call: `SELECT doc_id, metadata FROM documents WHERE doc_id IN (...)`. Indexed lookup, ≤K docs (K = result count, default 10). Adds ~1-2ms to the kb_search response. Skip the call when `len(hits) < 2` to avoid overhead on single-result queries.

## File-by-file changes

### Created

- `src/harbor_clerk/mcp_discriminator.py` — `_compute_discriminator_hint(hits, session) -> dict | None` + private helpers (`_find_differing_metadata_fields`, `_build_suggestion`). Pure post-processing; no global state.
- `tests/test_mcp_discriminator_hint.py` — unit tests for the trigger conditions, helper functions, output shape, and edge cases (empty metadata, all-same metadata, one-doc results, multi-field discrimination).
- `tests/test_mcp_tool_descriptions.py` — grep-style assertions that the rewritten descriptions hit the required surface (mention `metadata_filter`, mention `has_more`, mention decline conditions, mention `kb_batch_search` for the major-tier tools, etc.). Cheap regression-pin against future drift.

### Modified

- `src/harbor_clerk/mcp_server.py` — 16 docstring rewrites + one call site in `kb_search` that injects `discriminator_hint` into the response when non-`None`. Import of `_compute_discriminator_hint`. No other behavioral changes.

### Untouched

- `src/harbor_clerk/search.py` and `search_types.py` — the hybrid search algorithm and SearchHit shape stay as-is.
- `Document.metadata` schema — PR-F's column unchanged.
- All other kb_* implementations.

## Validation plan

1. **All existing tests pass.** Run `uv run pytest tests/` — should be 931 + new tests = ~945 pass, 0 fail.
2. **New unit tests:**
   - `test_mcp_discriminator_hint.py` covers ~8-10 tests: trigger when ambiguous + differing metadata, omit when single doc, omit when all metadata identical, omit when all metadata empty, multi-field discrimination ordering, suggestion string shape, edge case where one candidate has extra fields others lack, `_source_provenance` excluded from discrimination.
   - `test_mcp_tool_descriptions.py` covers ~6-8 grep assertions: kb_search description mentions `metadata_filter` + `discriminator_hint` + `has_more` + the decline-clause; kb_batch_search mentions "prefer over multiple sequential kb_search"; kb_get_document mentions `metadata` field; etc. These are mechanical pins against future drift.
3. **Ruff + format clean** on every changed file.
4. **Live re-run synthetic eval on both providers** (Sonnet via shim, gpt-4o via OpenAIProvider). Re-use PR-C's frozen `synthetic.yaml`. Expected impact:
   - Lookup correctness ↑ for the boundary-doc items (Pinnacle contracts, policy versions, marketing campaigns) — discriminator_hint + better descriptions should let models reach for `metadata_filter`.
   - Negative-item correctness ↑ (specifically `synth-neg-invoice-99999`) — explicit decline guidance in description should reduce fabrication.
   - Find under-iteration may or may not improve — descriptions can nudge but the eval items are bounded by what the model is willing to do. Realistic expectation: small movement.
5. **Cost note:** Re-eval is ~$10-15 LLM spend per provider; ~30 min wall-clock with the gpt-4o TPM limits. Same pre-authorized envelope as PR-C.

## Risks

- **Tool descriptions are read once at session start by most MCP clients.** A poorly-worded description that *worsens* behavior won't be caught until the live eval runs. Mitigation: ship descriptions + run eval before merge; if scores regress on any item, iterate before opening the PR.
- **`discriminator_hint`'s ε threshold is empirical.** `max(0.05, 0.1 * top_score)` is a starting point; the eval will tell us whether it triggers too often (noisy) or too rarely (misses real ambiguity). v2 could tune; v1 ships the heuristic.
- **`_find_differing_metadata_fields` traversal is naïve recursive descent.** Pathological deeply-nested metadata (e.g., a sidecar with 20-level nesting) could be expensive — but PR-F's sidecar 64KB cap bounds the worst case, and Tika/frontmatter outputs are flat 1-2 level dicts. Not a concern in practice; flag if a real-world doc hits it.
- **The hint's `suggestion` string is for LLMs to read.** If it's badly worded, models won't act on it. The string template is in `_build_suggestion`; iterate based on eval feedback.
- **A model that ignores `discriminator_hint` is no worse off than today.** The hint is additive; pre-PR-D behavior is preserved when the model doesn't read the field.

## Open questions (resolved)

These were the decisions made during the brainstorm — recording them inline so the spec is self-contained:

- **Bundle discriminator_hint into PR-D vs split.** Bundled. Rationale: descriptions teach models to USE the hint; the hint gives them something concrete to act on; single re-eval validates both.
- **Tool description rewrite scope.** All 16 tools, in tiered effort (major / moderate / light / admin) — consistent 4-part shape across all of them.
- **Response field redundancy.** `more_available` + `total_matching_estimate` are already shipped as `has_more` + `total_candidates`. PR-D's job is to make the descriptions tell models how to use them; no new response fields besides `discriminator_hint`.
- **Validation.** Both providers × synthetic. LLM spend pre-authorized in the original cloud-models-track approval.

## Out of scope (queued)

- **PR-E** — `kb_verify_identifier(identifier, kind?)` + `kb_documents_by_date(direction, query=None)`. Both benefit from PR-F's structured metadata.
- **PR-G** — harness audit (auto-analyze capture transcripts for tool-use patterns) + cross-judge sensitivity (re-judge a subset with gpt-4o instead of Sonnet).
- **Prompt-tuning experiment** — `DEFAULT_SYSTEM_PROMPT` in `scripts/test_corpora/runner/providers/base.py`. Separate from HC-side tool descriptions; queued after PR-G so we can measure prompt effects independently.
- **`metadata_filter` operators** (`>=`, `<`, `in`, `contains`) — v2 of PR-F's filter API.
- **Discriminator-by-text** (similarity-based discrimination when no structured metadata differs) — would need its own design, probably an LLM-based summarizer over the chunk-text differences.
- **`kb_get_document` `source_path` leak** — pre-existing security finding from PR-F's review; tracked separately in `pr_followups.md`.
- **`extract.py` early-return job-state leak** — pre-existing reliability finding from PR-F's review; tracked separately.
