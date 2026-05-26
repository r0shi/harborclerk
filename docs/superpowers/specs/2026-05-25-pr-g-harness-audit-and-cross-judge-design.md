# PR-G: Harness Audit + Cross-Judge Sensitivity — Design

**Date:** 2026-05-25
**Status:** spec — awaiting user review before plan + implementation
**Author:** Claude (with Alex)
**Companion docs:** PR-A/B/C answer-eval specs (`2026-05-22`, `2026-05-23`); PR-F (metadata extractors), PR-D (tool descriptions + discriminator_hint), PR-E (verify_identifier + documents_by_date) — all shipped.

## Goal

Make the answer-eval methodology trustworthy by adding two analyses that the harness doesn't have today:

1. **Harness audit** — auto-analyze captured tool-use transcripts to surface patterns (tool-use distribution, failure correlation, citation hygiene). The eval already records the raw data; today it requires manual inspection to learn anything from it.
2. **Cross-judge sensitivity** — re-judge a sample of captures with a second judge model (gpt-4o) and compare scores. Surfaces judge-bias and judge-noise risks in the existing Sonnet-only adjudication.

Both run as a standalone CLI script that reads the existing captures + verdicts and writes a new `audit.json` + `audit.md` alongside the existing `summary.json` / `detail.json`.

## Why this PR exists

The PR-A/B/C answer-eval framework gave Harbor Clerk concrete numbers about retrieval quality, which directly motivated PR-D/E/F. But the framework itself has two known blind spots:

- **The "why" behind a low score is hidden in the capture transcript.** When `synth-onboarding-mgr-0061` scores 1/5 on correctness, you can read the transcript by hand to see whether the model called `kb_search` once and gave up vs iterated 5 times. Hand-inspection doesn't scale across 80+ items × multiple models × multiple runs.
- **Sonnet is the only judge.** When the eval runs Sonnet-baseline + Sonnet-judge, judge bias toward Sonnet's style is a real concern. We've never measured it. gpt-4o is the obvious independent baseline.

PR-G addresses both without touching the existing eval modules. It's pure addition to `scripts/test_corpora/` — no risk to the running harness.

## Non-goals

- **No auto-suggest fixes from audit patterns.** The audit surfaces patterns; it doesn't generate "consider adding kb_documents_by_date" recommendations. Manual interpretation for v1.
- **No multi-judge ensemble.** No N-judge majority vote → trusted verdict logic. Pairwise compare with one alternative judge only.
- **No sweep-time auto-emission.** Standalone script invocation only; the existing `sweep.py` runner stays focused on running baselines.
- **No cross-run comparison.** One report per `--label`. Diffing two audits is a follow-up.
- **No locally-hosted judge.** Uses paid OpenAI. The provider abstraction makes a local judge a small follow-up if eval cost becomes a concern.
- **No self-consistency rounds.** Don't re-judge with Sonnet at a different temperature to separate "noisy judge" from "biased judge". Punted.

## Architecture

Three new files in `scripts/test_corpora/`. No changes to existing `runner/answer_judge.py`, `runner/answer_eval.py`, or `runner/sweep.py`.

**New files:**

- `scripts/test_corpora/runner/audit.py` (~200 LOC) — pure functions:
  - `tool_use_stats(captures: list[dict]) -> dict`
  - `failure_correlation(captures: list[dict], verdicts: list[dict]) -> dict`
  - `citation_hygiene(captures: list[dict]) -> dict`
  No I/O, no LLM calls. Easy to unit-test with fixture data.

- `scripts/test_corpora/runner/cross_judge.py` (~150 LOC):
  - `rejudge_with(captures, judge_provider, items=None) -> list[dict]` — one LLM call per item (or `items` sample), via the existing PR-C `Provider` interface.
  - `compare_judges(verdicts_a, verdicts_b) -> dict` — pure stats: per-dimension mean/std/min/max delta, Spearman rank correlation, Cohen's kappa, list of items with `|delta| >= 2` on any dimension.

- `scripts/test_corpora/audit_answer_eval.py` (~100 LOC) — CLI orchestrator. Parses args, loads captures + verdicts from disk, calls the two modules, writes outputs.

**New test files:**
- `scripts/test_corpora/tests/test_audit.py` — fixture-driven unit tests for the three audit functions.
- `scripts/test_corpora/tests/test_cross_judge.py` — fixture + MockProvider tests for `rejudge_with` + pure-data tests for `compare_judges`.

**Touched files:** none in `runner/` proper. Only the new files + test files.

**Why this split:** audit (read-only pattern analysis) and cross-judge (new LLM calls + stats) are conceptually distinct concerns. Splitting keeps each module under ~200 LOC and independently testable. Mirrors the existing `runner/answer_eval.py` (orchestrator) + `runner/answer_judge.py` (judge) split.

## `runner/audit.py`

### `tool_use_stats(captures: list[dict]) -> dict`

```python
{
  "total_captures": 30,
  "tool_call_distribution": {0: 1, 1: 4, 2: 12, 3: 8, "4+": 5},
  "tool_call_counts_per_tool": {
    "kb_search": 47,
    "kb_get_document": 12,
    "kb_documents_by_date": 0,
    "kb_verify_identifier": 0,
    ...
  },
  "captures_by_tool_count": {  # per-doc detail; used by failure_correlation
    "<qid>": {"tool_count": N, "tools_used": ["kb_search", "kb_search"]}
  }
}
```

The `"4+"` histogram bin keeps the structure stable across runs (we don't enumerate every possible call count). Per-tool counts include every kb_* tool name encountered.

### `failure_correlation(captures: list[dict], verdicts: list[dict]) -> dict`

Joins captures and verdicts by `question_id`. Surfaces actionable patterns:

```python
{
  "low_correctness_low_tool_use": [
    {"qid": "...", "tools_used": ["kb_search"], "correctness": 1, "title_fragment": "..."},
    ...
  ],
  "earliest_latest_questions_no_by_date_tool": [
    {"qid": "enron-lookup-earliest-california", "tools_used": ["kb_search"], "correctness": 0},
    ...
  ],
  "ambiguous_id_questions_no_verify_tool": [
    {"qid": "synth-onboarding-mgr-0061", "tools_used": ["kb_search"], "correctness": 1},
    ...
  ]
}
```

**Thresholds (v1, hardcoded; revisit if eval shows noise):**
- `low_correctness_low_tool_use`: `correctness <= 2` AND `tool_count <= 1`.
- `earliest_latest_questions_no_by_date_tool`: `qid` contains any of {`earliest`, `latest`, `oldest`, `newest`, `first`, `last`} AND `kb_documents_by_date` not in `tools_used`.
- `ambiguous_id_questions_no_verify_tool`: `correctness <= 2` AND verdict's `rationale` contains any of {`ambiguous`, `multiple`, `several`, `which`} (a lossy heuristic — flag, don't gate) AND `kb_verify_identifier` not in `tools_used`.

Each pattern returns an empty list (not a missing key) when no items match.

### `citation_hygiene(captures: list[dict]) -> dict`

For each capture, parse `tool_transcript[*].result_summary` (which is a JSON-encoded string of the tool's response) for `doc_id` values. Compare with `cited_doc_ids`:

```python
{
  "no_citations": [{"qid": ..., "answer_preview": "first 200 chars"}, ...],
  "fabricated_citations": [
    {"qid": ..., "cited": ["abc"], "seen_in_transcript": ["xyz", "def"]}, ...
  ],
  "grounded_count": 24,
  "total": 30
}
```

A capture is "grounded" if every `cited_doc_ids` value also appears somewhere in the parsed transcript doc_id set. "Fabricated" entries list both `cited` and `seen_in_transcript` to make manual triage easy.

**Limitation noted in the markdown:** the doc_id-extraction heuristic is "find any UUID-shaped string in the result_summary JSON." False positives possible (a query echoed in the result summary might contain a UUID); false negatives possible (a tool that doesn't include doc_id in its result summary). Good enough for surfacing obvious patterns; failures are corrected in interpretation.

## `runner/cross_judge.py`

### `rejudge_with(captures, judge_provider, items=None) -> list[dict]`

For each capture (optionally a random sample of `items`), constructs the same prompt the existing `AnswerJudge` uses (`_PROMPT` or `_PROMPT_FIND` depending on qtype) and calls `judge_provider`. Returns a list of verdict dicts in the existing shape:

```python
{
  "qid": "synth-...",
  "correctness": 4,
  "groundedness": 5,
  "completeness": 3,
  "rationale": "...",
  "judge_model": "gpt-4o",
}
```

Sampling uses a fixed seed (run-time, not hardcoded) so re-running with the same `items` gives the same sample.

`judge_provider` is the existing PR-C `Provider` interface. For v1 the only non-Sonnet judge is the OpenAI provider (`gpt-4o`); future judges (Gemini, local model) plug in with no changes to this module.

**Reusing the judge prompts:** `cross_judge.py` imports `_PROMPT` and `_PROMPT_FIND` from `runner.answer_judge` directly. If the existing judge's prompt changes, the cross-judge naturally tracks. This is the same prompt-text contract `AnswerJudge` enforces; we're swapping the model, not the rubric.

### `compare_judges(verdicts_a, verdicts_b) -> dict`

Joins by `question_id`. Computes per-dimension stats and surfaces disagreements:

```python
{
  "n": 30,
  "judges": ["claude-sonnet-4-6", "gpt-4o"],
  "deltas": {
    "correctness": {"mean": -0.13, "std": 0.84, "max": 2, "min": -2},
    "groundedness": {"mean": 0.27, "std": 0.61, "max": 3, "min": -1},
    "completeness": {"mean": -0.07, "std": 0.92, "max": 2, "min": -3}
  },
  "spearman": {"correctness": 0.78, "groundedness": 0.81, "completeness": 0.73},
  "kappa": {"correctness": 0.42, "groundedness": 0.51, "completeness": 0.38},
  "disagreements": [
    {
      "qid": "synth-...",
      "judge_a": {"correctness": 4, "groundedness": 5, "completeness": 3, "rationale": "..."},
      "judge_b": {"correctness": 2, "groundedness": 5, "completeness": 2, "rationale": "..."},
      "max_delta": 2,
      "delta_dim": "correctness"
    }
  ]
}
```

**Stats implementation:** `compare_judges` implements Spearman and Cohen's kappa by hand (small dataset, no need to drag in scipy). The implementation has its own unit tests against known inputs to avoid a regression in the stats themselves.

**Delta sign convention:** `b - a` (judge_b minus judge_a). Documented in the markdown so readers know which way "positive delta" points.

## CLI + output format

### `scripts/test_corpora/audit_answer_eval.py`

```
USAGE
  audit_answer_eval.py --label <run-label> [options]

REQUIRED
  --label LABEL            The run label whose captures + verdicts to analyze.
                           Maps to <workdir>/answer-eval/{captures,verdicts}/<corpus>/<baseline-model>/

OPTIONAL
  --workdir DIR            Default: $HARBOR_CLERK_WORKDIR
                           OR ~/Library/Application Support/Harbor Clerk
  --corpus CORPUS          synthetic | cuad | enron. Required if multiple exist
                           under the captures dir; auto-resolved if only one.
  --baseline-model MODEL   Auto-resolved if only one model dir exists under the corpus;
                           required otherwise.
  --cross-judge MODEL      e.g. "gpt-4o". If provided, runs rejudge_with()
                           against this model and computes compare_judges().
                           Without this flag, only the static audit runs.
  --rejudge-sample N       Re-judge only N captures (random sample with seed).
                           Useful for smoke-testing the cross-judge wiring.
  --skip-static            Skip the static audit (useful when iterating only
                           on cross-judge with --cross-judge).
  --output-dir DIR         Override output location. Default:
                           <workdir>/answer-eval/reports/<label>/
```

**Output `audit.json`:**

```jsonc
{
  "label": "synthetic-phase2b",
  "corpus": "synthetic",
  "baseline_model": "claude-sonnet-4-6",
  "generated_at": "2026-05-25T...",
  "tool_use": { /* tool_use_stats output */ },
  "failure_correlation": { /* failure_correlation output */ },
  "citation_hygiene": { /* citation_hygiene output */ },
  "cross_judge": null  // or compare_judges output if --cross-judge supplied
}
```

**Output `audit.md`** is human-readable:

```markdown
# Audit — synthetic-phase2b (claude-sonnet-4-6)

Generated 2026-05-25T...

## Tool-use distribution
| tool calls | count |
| ---        | ---   |
| 0          | 1     |
| 1          | 4     |
| ...        | ...   |

| tool                    | calls |
| ---                     | ---   |
| kb_search               | 47    |
| kb_get_document         | 12    |
| kb_documents_by_date    | 0     |
| ...                     | ...   |

## Failure correlation
### Low correctness + low tool use (N items)
- synth-... — correctness 1, called [kb_search] once
- ...

### Earliest/latest questions, no kb_documents_by_date (N items)
- enron-lookup-earliest-california — correctness 0
- ...

### Possibly-ambiguous questions, no kb_verify_identifier (N items)
- ...

## Citation hygiene
- N/T grounded (every cited doc_id appears in the tool transcript)
- M items with no citations
- K items with fabricated citations (cited doc_id not seen in any tool response)

## Cross-judge (if --cross-judge was supplied)
Judges: claude-sonnet-4-6 vs gpt-4o (delta = gpt-4o - sonnet)
| dim          | mean Δ | std | min | max | spearman | kappa |
| ---          | ---    | --- | --- | --- | ---      | ---   |
| correctness  | -0.13  | ... | ... | ... | 0.78     | 0.42  |
| groundedness | 0.27   | ... | ... | ... | 0.81     | 0.51  |
| completeness | -0.07  | ... | ... | ... | 0.73     | 0.38  |

### Top disagreements (|Δ| >= 2)
- synth-... — correctness 4 (sonnet) vs 2 (gpt-4o)
  - sonnet: "..."
  - gpt-4o: "..."
- ...
```

### Error handling

- Missing `--workdir`/captures/verdicts → clear stderr error, exit 1.
- `--cross-judge` requested but `OPENAI_API_KEY` not set → exit 1; error message names the env var.
- Single provider error mid-rejudge (429, transient) → continue. The affected item gets `judge_error: "..."` in the result list; the markdown surfaces a count of `N items skipped due to judge errors`.
- Multi-corpus or multi-model dirs detected without explicit `--corpus`/`--baseline-model` flags → exit 1; list the choices.
- Empty captures dir → exit 0 with `total_captures: 0` audit; markdown notes "no captures found."

## Testing

### `scripts/test_corpora/tests/test_audit.py` (~10 tests)

- `tool_use_stats`:
  - empty captures → zero-valued shape (`{"total_captures": 0, ...}`)
  - single-capture single-tool → distribution `{1: 1}`
  - mixed captures → distribution histograms correct, per-tool counts correct, captures with `tool_count >= 4` bucket as `"4+"`
- `failure_correlation`:
  - `low_correctness_low_tool_use` surfaces only items meeting both thresholds
  - `earliest_latest_questions_no_by_date_tool` matches `qid` containing one of the 6 substrings AND missing the tool
  - `ambiguous_id_questions_no_verify_tool` uses the rationale heuristic
  - empty match-sets return `[]` (not missing keys)
- `citation_hygiene`:
  - capture with cited_doc_ids ⊆ seen-in-transcript → grounded
  - capture with cited_doc_ids ⊄ seen → in `fabricated_citations`
  - capture with empty `cited_doc_ids` → in `no_citations`
  - `grounded_count` + `len(no_citations)` + `len(fabricated_citations)` = `total`

### `scripts/test_corpora/tests/test_cross_judge.py` (~8 tests)

- `rejudge_with`:
  - MockProvider returning canned `RejudgeResult` shape → result list has expected shape
  - `items=N` with `len(captures) > N` → returns at most N items, deterministically with a seed
  - One provider error → that item's result has `judge_error` set; other items succeed
- `compare_judges`:
  - empty inputs → `{"n": 0, ...}` shell
  - identical verdicts → mean delta 0, spearman 1.0, kappa 1.0
  - one item with `|delta| >= 2` on at least one dim → appears in `disagreements`
  - mismatched qids (one judge has extras) → intersection only, n correct
  - Spearman + kappa unit tests with hand-computed expected values on a 4-item fixture

### Live smoke (Task N in the plan, deferred from CI)

```
audit_answer_eval.py --label synthetic-phase2b --cross-judge gpt-4o --rejudge-sample 3
```

Confirms end-to-end: file discovery, audit functions on real data, OpenAI provider integration, output writing, markdown rendering. Requires `OPENAI_API_KEY`; user runs manually.

## Decisions (closed during brainstorming)

- **Scope: one PR for both subsystems.** Both pure additions to `scripts/test_corpora/`; conceptually distinct but tightly aligned in purpose. Splitting would double review cycles for marginal blast-radius reduction.
- **Audit patterns chosen: tool-use distribution + failure correlation + citation hygiene.** Query diversity and tool-misuse explicitly deferred. The chosen three give the highest signal for the typical "did the model reach for the right tools" question.
- **Audit CLI shape: standalone script.** No flag on the sweep runner. Decouples sweep timing from audit logic iteration.
- **Audit output: JSON + Markdown side-by-side.** JSON for follow-up tooling; Markdown for reading + PR descriptions.
- **Cross-judge model: gpt-4o only.** Single alternative judge for v1. Multi-judge ensemble + self-consistency rounds deferred.
- **Cross-judge sample: full re-judge on one (corpus, baseline) pair.** Synthetic × Sonnet baseline × gpt-4o judge ≈ 30 items, ≈ $0.15. Enough N for meaningful stats; expand only if a real signal appears.
- **Stats implementation: by hand (no scipy dep).** Small dataset; the stats have their own unit tests against known inputs.
- **Reuse `_PROMPT` / `_PROMPT_FIND` directly** from `runner.answer_judge`. Cross-judge swaps the model, not the rubric.
- **Module split: audit + cross_judge separate.** Each conceptually distinct, each under ~200 LOC, each independently testable.

## Out of scope / follow-ups

- **Auto-suggest concrete fixes from failure_correlation** — pattern surfacing only for v1.
- **Multi-judge ensemble + voting** — pairwise only for v1.
- **Sweep-time auto-emission** — standalone only.
- **Cross-run comparison** — single-report-per-invocation only.
- **Locally-hosted judge** — provider abstraction supports it; OpenAI only for v1.
- **Self-consistency cross-judge** — separates judge noise from cross-provider bias; punted.
- **Query-diversity audit pattern** — edit-distance / unique-tokens across consecutive kb_search calls.
- **Tool-misuse audit pattern** — count of error JSON responses per tool.
- **Audit threshold tuning** — `correctness <= 2`, `tool_count <= 1`, the 6 earliest/latest substrings, the 4 ambiguity rationale substrings are all hardcoded heuristics. Revisit if eval surfaces false positives/negatives.

## Open questions / risks

- **doc_id extraction from `result_summary` is regex-based.** The result_summary is a JSON-encoded string of the tool response; we parse with a UUID-shape regex rather than re-parsing the JSON, because tool response shapes vary. False positives (a query string echoed in result_summary contains a UUID) and false negatives (a tool that omits doc_id) are both possible. Documented in markdown; treat as a heuristic, not ground truth.
- **Rationale-string heuristics in `ambiguous_id_questions_no_verify_tool` are brittle.** Triggering on {ambiguous, multiple, several, which} catches the obvious cases but misses paraphrases. Acceptable for v1; revisit with a more principled signal (e.g., the verdict's `discriminating_fields` from PR-D's hint if it ever populates them per-item) if the heuristic underperforms.
- **Stats by hand instead of scipy.** Spearman and kappa are 20-line functions; risk is a subtle math bug in `compare_judges`. Mitigated by unit tests against hand-computed values on a 4-item fixture.
- **Cross-judge cost.** ~$0.15 per full re-judge of 30 items via gpt-4o. Not a blocker; documented so the user doesn't accidentally run it 100×.
