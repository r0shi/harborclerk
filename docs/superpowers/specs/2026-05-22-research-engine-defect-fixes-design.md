# Research-engine defect fixes — design

Tracking issue: [#368](https://github.com/r0shi/harborclerk/issues/368)

## Context

Analysis of the `2026-05-05-prod` test-corpora sweep surfaced three defects in
Harbor Clerk's research engine (`src/harbor_clerk/llm/research.py`). They
explain why local models scored far worse on research questions than on ask
questions (research 94% fail / 0 pass vs ask 76% fail). These are product
bugs — research is a shipping feature, so real users on local models hit the
same behavior.

This design fixes all three in one PR. Out of scope: judge-verdict
calibration (a separate scoring concern), the DeepSeek SSE-watchdog (lives in
the test harness, not the research engine), and selectable citation methods
(deferred — see the `research-citations-selectable-methods` note).

## Fix 1 — Drop corpus-seeded queries

### Problem

`_seed_queries_from_corpus` (research.py:457) builds queries by concatenating
a corpus entity with the question's first four long words:
`seeded.append(f"{name} {q_short}")`. The result is ungrammatical word salad —
`"Party Identify major vendor relationships"`,
`"0001Invoice Compare quarterly performance trends"`. `_plan_queries` merges
~7-8 of these into every research run alongside the ~7 good LLM-planned
queries. Confirmed systematic across all corpora and models. The area was
already patched once (the `CARDINAL`/`ORDINAL` filtering); the
`{entity} {verb-phrase}` strategy itself is the defect.

### Change

- Delete `_seed_queries_from_corpus` and the `_SEED_QUERY_BLOCKED_ENTITY_TYPES`
  constant. The `entity_overview` tool call inside the function goes away.
- `_plan_queries`: drop the seeded-merge. The returned query list is the
  LLM-planned queries only — deduped, capped at `depth_config["max_queries"]`.
- Keep the existing keyword fallback (research.py:547-553) for when the LLM
  produces no plausible queries.
- Raise `llm_target` from `max(5, max_queries // 2)` to the full
  `depth_config["max_queries"]`. The LLM is now the sole query source and is a
  far better generator than entity-concatenation; ask it for the full budget.
  `_is_plausible_query` still filters junk.

### Files

`src/harbor_clerk/llm/research.py`

## Fix 2 — Don't let "No relevant findings" be terminal

### Problem

`_extract_notes` may return the sentinel
`No relevant findings in this passage set.` — the note-extraction prompt's
anti-hallucination escape hatch. A weak model takes that shortcut even when
retrieval surfaced clearly-relevant documents (observed: `synthetic-research-1`
retrieved `0265_quarterly_report` at score 1.72 for a quarterly-performance
question, then the model returned the sentinel). The sentinel becomes the
notes, and synthesis answers "the corpus did not yield evidence."

### Change

In `research_stream`, immediately after the round-1 `_extract_notes` call
(research.py ~1162):

1. **Detect the sentinel** — match tolerantly:
   `notes_text.strip().lower().startswith("no relevant findings")`. This is
   distinct from `_extract_notes`'s genuinely-empty return
   (`"No relevant passages were found in the corpus."`, research.py:818),
   which must NOT trigger a retry.
2. **Compute top retrieval score** — `max(c["score"] for c in coverage.values())`.
3. **If sentinel AND top score ≥ `_RETRIEVAL_RELEVANCE_FLOOR`:** re-run
   `_extract_notes` once with `forceful=True`.
4. **If the retry also returns the sentinel:** raw-passage fallback —
   `notes_text = f"## Raw passages\n{passages_text[:_NOTE_PROMPT_CHAR_CAP]}"`
   (the existing fallback form already used for the time-budget and
   LLM-error cases).
5. **If top score < floor:** leave the sentinel as the notes. The corpus
   genuinely lacks the information; the honest "no findings" answer is correct.

Supporting changes:

- `_extract_notes` gains a `forceful: bool = False` parameter. When true it
  uses a new system-prompt constant `_NOTE_EXTRACTION_SYSTEM_FORCEFUL` — the
  same extraction rules, but prefaced with: these passages are the
  top-ranked retrieval matches for the question and are very likely relevant;
  extract the findings; return "No relevant findings" only if the passages
  genuinely contain nothing on-topic, not as a shortcut.
- New module constant `_RETRIEVAL_RELEVANCE_FLOOR`. Provisional value 1.0;
  set the final value after sanity-checking the coverage-score distributions
  in the run data. Tunable.
- Scope: round-1 note extraction only. Gap-round note extraction
  (research.py ~1280) is supplementary and stays unchanged.

### Files

`src/harbor_clerk/llm/research.py`

## Fix 3 — Populate `result.citations`

### Problem

A research response's `result.citations` is always `[]`. Root cause:
`routes/research.py:147-161` computes citations at read-time via
`extract_citations_from_tool_result` over the conversation's messages. But
`research_stream` does retrieval internally (`_search_fan_out`,
`_read_evidence`) and saves only one assistant message — the report. There are
no tool-result messages, so nothing is extracted.

### Change

Chosen semantics: `citations` = the documents that informed the answer — the
distinct docs whose passages `_read_evidence` selected into `passages_text`.

- `_read_evidence` (research.py:714): in addition to the formatted
  `passages_text`, return the metadata of the selected chunks' docs
  (`doc_id`, `doc_title`, `page`). Change the return type to
  `(passages_text, evidence_docs)`; update both call sites (round-1 read and
  the gap-round read at research.py ~1266).
- `research_stream`: accumulate `evidence_docs` across the round-1 read and
  any gap-round reads; dedupe by `doc_id`.
- When `research_stream` saves the assistant `ChatMessage` (research.py:1359),
  set `rag_context` to `{"citations": [{doc_id, doc_title, page}, ...]}`.
  `ChatMessage.rag_context` is an existing JSONB column — **no migration**.
- `routes/research.py`: for the research turn, read citations from the
  assistant message's `rag_context["citations"]` instead of
  `extract_citations_from_tool_result`. Keep the `dedupe_citations` pass.
- `schemas/research.py:63-67`: update the now-stale comment ("derived from the
  tool-result messages").

### Files

`src/harbor_clerk/llm/research.py`, `src/harbor_clerk/api/routes/research.py`,
`src/harbor_clerk/api/schemas/research.py` (comment only)

## Testing

- **Fix 1:** `_plan_queries` returns only LLM-planned queries; no seeded
  entries; the keyword fallback still fires when the LLM yields nothing.
- **Fix 2:** sentinel + high top-score → retry happens; sentinel returned
  twice → notes become the raw-passage block; sentinel + low top-score →
  sentinel kept, no retry. Mock `_extract_notes` / `_llm_complete`.
- **Fix 3:** `_read_evidence` returns the selected-doc set; `research_stream`
  writes it into `rag_context`; `routes/research.py` surfaces it on
  `result.citations`.
- The existing research test suite stays green.

## Packaging

One PR. A fresh-eyes `feature-dev:code-reviewer` pass on the branch tip
before opening the PR, per the standing review directive.
