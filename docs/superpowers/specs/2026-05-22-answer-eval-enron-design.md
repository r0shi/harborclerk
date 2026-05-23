# PR-A: Enron Answer-Eval + Judge `find` Type — Design

**Status:** Approved design. Ready for an implementation plan.
**Date:** 2026-05-22
**Companion docs:**
- `2026-05-22-real-eval-design-sketch.md` — overall direction (phase 1).
- `2026-05-22-real-eval-phase1-design.md` — phase 1 design (CUAD).

---

## 1. Goal

Extend `--mode answer-eval` from one corpus (CUAD, phase 1) to a second — **Enron** — and introduce a third question type, `find`, alongside the existing `lookup` and `negative`. Together these prove the eval is corpus-agnostic and validate Sonnet + HC's MCP on email search, not just contract clause-extraction.

PR-A of a three-PR phase-2 sequence:
- **PR-A** (this doc): Enron + judge `find` type.
- **PR-B**: Synthetic corpus.
- **PR-C**: OpenAI / multi-model.

## 2. Scope

- **Corpus**: Enron (10,576 docs already ingested; folder-scoped read-only API key already minted).
- **Question types**:
  - `find` — return-a-set (e.g., "Find emails about Raptor"). **New type.**
  - `lookup` — single fact (e.g., "What was the date of the earliest California email?"). Existing, unchanged.
  - `negative` — `find` with `count == 0`; tests over-hedging behaviour.
- **Items**: ~11 total — 6 `find` + 2 `lookup` + 3 `find`-negatives.
- **Model under test**: Sonnet 4.6 (multi-model deferred to PR-C).

## 3. Why Enron + raw-filesystem ground truth

Phase 1 §4 — *ground truth must be independent of the MCP*. Enron has no shipped expert label set like CUAD's `master_clauses.csv`. The clean alternative: derive truth by **grepping the raw filesystem** (`<workdir>/enron/ingest/*.eml`) + Python `email` parsing.

Validated empirically:

| Search | Hits |
|---|--:|
| Raptor | 4 |
| LJM | 21 |
| off-balance-sheet | 17 |
| Arthur Andersen | 21 |
| FERC | 51 |
| Lay forwarded 2001 (heuristic) | ~126 |

All tractable. Grep gives the *exhaustive* relevant-doc set per question — which is precisely what makes coverage (≈ recall) measurable.

## 4. Architecture — components

```
<workdir>/enron/ingest/*.eml
        │  generator (explicit, one-shot)
        ▼
groundtruth/enron.yaml          ← frozen, version-controlled
        │  answer-eval runner (existing, --corpora enron)
        ▼
captures + verdicts (model-keyed, reused by default — existing)
        │
        ├── find items:        completeness ← compute_coverage(cited, truth.all)
        └── lookup/negative:   completeness ← judge
        ▼
summary.json + detail.json (per-label, existing)
```

Three additive changes:

1. **`groundtruth/generate_enron.py`** — new generator.
2. **`AnswerJudge._PROMPT` branches by `qtype`** — `find` gets a different template.
3. **`runner/answer_eval.run` overrides `completeness` for `find` items.**

The runner, sweep wiring, capture format, reuse mechanism — **all unchanged**. PR-A is additive only.

## 5. The `find` answer_key shape

```yaml
- id: enron-find-raptor
  question: "Find emails about Raptor."
  type: find
  answer_key:
    count: 4
    all:
      - skilling-j_inbox_1109_.eml
      - lay-k_inbox_268_.eml
      - lay-k_inbox_422_.eml
      - shackleton-s_all_documents_4047_.eml
    sample:                 # ≤ K entries for the judge prompt
      - skilling-j_inbox_1109_.eml
      - lay-k_inbox_268_.eml
      ...
```

- `count`: integer, total truth-set size.
- `all`: complete list of relevant doc titles (filename stems); used for coverage math.
- `sample`: top-K (K=10 default) for rendering in the judge prompt; spares the prompt from 100+ titles.

For `find`-negatives: `count: 0`, `all: []`, `sample: []`.

`GTItem.answer_key` is broadened to `str | dict | None`. Loader validates the dict shape for `find` items.

## 6. Coverage scoring (deterministic + judge hybrid)

For `find` items, the runner computes completeness programmatically after the judge returns:

```python
def compute_coverage(cited: list[str], truth_all: list[str]) -> int:
    if not truth_all:                          # negative: citing nothing is correct
        return 5 if not cited else max(0, 5 - len(cited))
    overlap = len(set(cited) & set(truth_all))
    return round(overlap / len(truth_all) * 5)
```

For `lookup` and `negative` (the original CUAD-style negative on a lookup): judge scores all 3 dimensions, `source` empty.

Rationale: coverage on `find` is a precise set overlap, not a fuzzy judgement. Asking an LLM to score 0–5 for what's objectively measurable adds noise. Correctness (does the answer narrative reflect the right docs) and groundedness (no fabricated citations) stay LLM-judged — they're appropriately fuzzy.

## 7. The ground-truth set

`groundtruth/enron.yaml` — ~11 items:

| id | type | derivation |
|---|---|---|
| `enron-find-raptor` | find | grep `raptor` (case-insensitive) |
| `enron-find-ljm` | find | grep `ljm` |
| `enron-find-offbalancesheet` | find | grep `off.balance.sheet` |
| `enron-find-arthurandersen` | find | grep `arthur.andersen` |
| `enron-find-ferc` | find | grep `ferc` |
| `enron-find-layforwarded2001` | find | filter on From: lay + 2001 + Fw/Fwd subject |
| `enron-lookup-earliest-california` | lookup | grep `california` + parse Date + min (after sentinel filter); `answer_key` = `"YYYY-MM-DD"` |
| `enron-lookup-skilling-last-pre-resign` | lookup | filter From: Skilling + Date < 2001-08-14 + max; `answer_key` = subject string |
| `enron-find-neg-cryptocurrency` | find | grep returns 0 |
| `enron-find-neg-bitcoin` | find | grep returns 0 |
| `enron-find-neg-spacex` | find | grep returns 0 |

Generator runs deterministically; a single human curation pass commits the frozen `enron.yaml` (same discipline as CUAD).

**Brittleness notes:**
- `enron-lookup-skilling-last-pre-resign` encodes external knowledge (Skilling resigned 2001-08-14; addresses include `jeff.skilling@enron.com`). Human curation catches drift.
- Sentinel-date filter: discard `.eml` `Date` headers earlier than `1995-01-01` (PST extractions stub `1980-01-01` etc.). Validated against the live corpus before locking the cutoff.

## 8. The judge change

`AnswerJudge._PROMPT` becomes type-aware. For `find`:

```
QUESTION TYPE: find
The ground-truth answer key is the exhaustive list of relevant documents
(count: {count}). A representative sample is shown below. The runner
computes coverage (recall) separately, so SET completeness=0 and let the
runner override it.

GROUND-TRUTH SAMPLE ({sample_size} of {count} relevant docs):
{rendered_sample}

Score correctness (does the assistant's answer narrative reflect the right
documents?) and groundedness (does the assistant cite real, relevant
documents without fabrication?). Set completeness=0.
```

Judge response shape unchanged (3 ints + rationale). The runner overrides.

For `lookup` and `negative` — prompt unchanged from phase 1.

## 9. The runner change

`runner/answer_eval.run()` adds a small block after `judge.judge_answer(...)`:

```python
if item.type == "find":
    coverage = compute_coverage(
        capture.get("cited_doc_titles", []),
        item.answer_key.get("all", []),
    )
    verdict = dataclasses.replace(
        verdict,
        completeness=coverage,
        source={**verdict.source, "completeness": "deterministic"},
    )
```

`AnswerVerdict` gains `source: dict[str, str] = dataclasses.field(default_factory=dict)`. Back-compat: existing serialized verdicts deserialize cleanly (default empty dict).

## 10. Harness integration

Zero CLI changes. Existing command works:

```bash
HC_API_KEY=<enron-scoped> ANTHROPIC_API_KEY=<...> \
  uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id answer-eval --mode answer-eval \
  --corpora enron --models claude-sonnet-4-6 --label enron-phase2a \
  --workdir "$WD" --api-base http://localhost:8100
```

The `--corpora enron` argument routes to `<workdir>/answer-eval/{captures,verdicts}/enron/<model>/…`.

## 11. De-risk before the full run

1. Run `generate_enron.py` against the live corpus; eyeball `enron.yaml`. Sanity-check the counts and a few doc titles.
2. Spot-check 2 items via a manual `kb_search` (or HC's `/api/search`) — confirm those doc titles exist in HC's index.
3. Run a single item through `--mode answer-eval` (using the existing `--refresh` discipline on one id) and inspect the verdict before launching all ~11.

## 12. Output

Same per-label report. The summary's `by_type` now includes a `find` bucket; `detail.json` records may include `source: {"completeness": "deterministic"}` for `find` items.

```json
{
  "overall": {"n": 11, "correctness": 4.6, "groundedness": 4.1, "completeness": 4.0},
  "by_type": {
    "find":     {"n": 6, ...},
    "lookup":   {"n": 2, ...},
    "negative": {"n": 3, ...}
  }
}
```

## 13. Testing

- **`test_generate_enron.py`** — fixture: ~6 `.eml` files in `tmp_path`. Assertions: generator emits expected items; `answer_key.count` matches; gold doc titles appear in `all`; sentinel-date filter discards the bad-date message.
- **`test_answer_judge.py`** — extend with a `find`-type case; assert the prompt contains `count` + sample; verify the response parses to a verdict.
- **`test_compute_coverage`** (inline in `test_answer_eval.py` or its own file) — edge cases: empty truth + empty cited (=5), empty truth + non-empty cited (penalty), partial overlap, exact match, over-cite.
- **`test_answer_eval.py`** — extend the existing reuse test with a `find` item; verify `verdict.completeness` matches `compute_coverage` (not the FakeJudge's value); verify `verdict.source["completeness"] == "deterministic"`.

Operational (post-PR): Task-8-equivalent run against the live HC + Enron-scoped key (already in hand). Compare against the CUAD baseline.

## 14. Out of scope (deferred)

- Aggregation/synthesis questions (the `research` items + `ask-1`, `ask-6` in the existing `questions/enron.yaml`) — phase 3, separate judge shape.
- Synthetic corpus (PR-B), OpenAI / multi-model (PR-C).
- CUAD generator backfill (content-aware category-type table, stride-sampling for diversity) — tracked in `pr_followups.md`.

## 15. Open questions / decisions deferred to the plan

- **Negative search terms** — `cryptocurrency`, `bitcoin`, `spacex` are placeholders; pick during implementation based on what unambiguously returns 0 hits and reads naturally.
- **Coverage → 0–5 mapping** — `round(coverage * 5)` is the simple choice; the plan can refine if edge cases warrant explicit thresholds.
- **Sample size `K`** — default 10; tune if the judge prompt feels under- or over-stuffed.
