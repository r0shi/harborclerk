# PR-B: Synthetic Answer-Eval — Design

**Status:** Approved design. Ready for an implementation plan.
**Date:** 2026-05-23
**Companion docs:**
- `2026-05-22-real-eval-design-sketch.md` — overall direction.
- `2026-05-22-real-eval-phase1-design.md` — phase 1 design (CUAD).
- `2026-05-22-answer-eval-enron-design.md` — phase 2a design (Enron + `find` type).

---

## 1. Goal

Extend `--mode answer-eval` to a third corpus — **Synthetic** (the bilingual Marbledock & Associates fixture) — leveraging the **JSON sidecars authored at generation time** as canonical, authoritative ground truth. Validates that the answer-eval is corpus-agnostic across structured-fact retrieval, contract clause-extraction (PR-A: CUAD), email search (PR-A: Enron), and structured-business-document lookups (this PR).

PR-B of the phase-2 sequence — sibling to PR-A (Enron, merged in #385) and PR-C (OpenAI / multi-model, upcoming).

## 2. Scope

- **Corpus:** Synthetic (280 docs already ingested in HC; folder-scoped read-only API key already minted).
- **Question types** (all inherited from PR-A; no new types):
  - `lookup` — single-fact extraction from a single doc (bulk of items).
  - `find` — cross-doc return-a-set queries (e.g. "Find invoices over $5,000"). 2–3 items.
  - `negative` — entities/values absent from the corpus. 2–3 items, mix of `lookup`-negative and `find`-negative.
- **Items:** ~20 total — all 9 doc types represented, plus 2–3 French items, 2–3 negatives.
- **Model under test:** Sonnet 4.6 (multi-model deferred to PR-C).

## 3. Why Synthetic + sidecar ground truth

The synthetic corpus generator (`corpora/synthetic.py`) writes a per-doc **JSON sidecar with structured ground-truth facts** at generation time:

```
<workdir>/synthetic/ingest/0019_invoice.txt   ← rendered (or .pdf for the OCR subset)
<workdir>/synthetic/ingest/0019_invoice.json  ← canonical sidecar
```

The sidecar carries doc-type-specific fields:

| doc-type | sidecar keys |
|---|---|
| `invoice` | `vendor`, `invoice_number`, `date`, `total_usd`, `line_items` |
| `board_minutes` | `date`, `attendees`, `decisions`, `lang` |
| `onboarding_letter` | `employee_name`, `role`, `start_date`, `languages_used`, `signing_manager` |
| `quarterly_report` | `quarter`, `year`, `revenue_usd`, `key_initiatives` |
| (5 others) | per-type shapes |

This is the cleanest possible ground-truth source — **the corpus author wrote it**. No grep, no LLM judgment, no inference. The sidecar value *is* the truth. Per phase 1 §4 (truth independent of the MCP), reading the sidecar — not HC's index — is correct.

## 4. Architecture — components

```
<workdir>/synthetic/ingest/<id>_<doctype>.json (sidecars)
        │  generator (explicit, one-shot)
        ▼
groundtruth/synthetic.yaml      ← frozen, version-controlled
        │  answer-eval runner (existing, --corpora synthetic)
        ▼
captures + verdicts (model-keyed, reused by default — existing)
        │  judge + (for find items) compute_coverage override — existing
        ▼
summary.json + detail.json (per-label, existing)
```

**One additive change only:** the new generator. Zero changes to `runner/answer_eval.py`, `runner/answer_judge.py`, `runner/sweep.py`, or `GTItem`. PR-B is the smallest of the three phase-2 PRs by design — synthetic ground truth is structurally easier than Enron's grep-derived truth or CUAD's CSV-derived truth.

## 5. Generator structure — per-doc-type recipes

`scripts/test_corpora/groundtruth/generate_synthetic.py` follows PR-A's recipe-table pattern, but dispatches on doc-type (from filename — `0019_invoice.json` → `"invoice"`) rather than per-question:

```python
def _recipe_invoice(sidecar: dict, stem: str) -> list[dict]:
    inv = sidecar["invoice_number"]
    return [
        _lookup_item(
            id_=f"synth-invoice-total-{inv}",
            question=f"What is the total amount of invoice {inv}?",
            gold_doc=stem,
            answer_key=f"${sidecar['total_usd']:.2f}",
            clause_category="invoice",
        ),
        _lookup_item(
            id_=f"synth-invoice-vendor-{inv}",
            question=f"Who is the vendor on invoice {inv}?",
            gold_doc=stem,
            answer_key=sidecar["vendor"],
            clause_category="invoice",
        ),
    ]

RECIPES = {
    "invoice": _recipe_invoice,
    "board_minutes": _recipe_board_minutes,
    "onboarding_letter": _recipe_onboarding_letter,
    "quarterly_report": _recipe_quarterly_report,
    "vendor_contract": _recipe_vendor_contract,
    "internal_memo": _recipe_internal_memo,
    "policy_doc": _recipe_policy_doc,
    "marketing_brief": _recipe_marketing_brief,
    "employee_handbook": _recipe_employee_handbook,
}
```

**Selection:** for each doc-type, take the first `N` sidecars by sorted filename (`N=2` default), apply the recipe, collect items. Deterministic by construction. Yields ~14 lookup items.

**Cross-doc finds:** 2 hand-coded recipes that walk all sidecars and apply a filter:
- `_recipe_find_invoices_over_5000` → emits a `find` item whose `answer_key.all` is every invoice stem with `total_usd > 5000`.
- One more cross-doc find (TBD during implementation — likely "Find board minutes where Veltrane is mentioned" or similar, derived from `decisions` text).

**Negatives:** 2 hand-coded items with answer_keys derived to be definitively absent:
- `_recipe_neg_invoice_number` — `answer_key=None`, `type=negative`: "What is the total of invoice INV-99999?" (no invoice has that number — verified at generation time).
- `_recipe_find_neg_vendor` — `answer_key={count:0, all:[], sample:[]}`, `type=find`: "Find vendor contracts with Globex Aerospace" (the corpus's known vendors are listed in `corpora/synthetic.py:COMPANY["key_vendors"]`; pick a name not in that list).

The generator verifies negatives match zero docs in the actual corpus before emitting (same fail-fast guard pattern as PR-A's `NEGATIVE_TERMS` check).

## 6. Bilingual coverage

Synthetic is bilingual by design (Toronto/Montréal company). Some sidecars carry `lang` or `languages_used` markers. **Target 2–3 French-language items** as a subset of the lookup pool:

- A recipe variant picks an FR-tagged doc (e.g. `lang: "fr"` on a `board_minutes`, or a `policy_doc` rendered in French) and emits the question in French with a French answer_key.
- Example: `"Quelle est la date du procès-verbal du conseil ?"` → answer_key in French (e.g., `"2 avril 2025"`).
- The judge (Sonnet 4.6) handles French natively; no judge change.
- Tests HC's bilingual retrieval path: dual `fts_en`/`fts_fr` columns + multilingual embedding.

If the generator can't find ≥2 FR-tagged sidecars during execution, the plan documents the fallback (skip FR items, log a warning).

## 7. Item composition — ~20 items

| Source | Items | Type |
|---|--:|---|
| 9 doc-type recipes × ~1.5 items each | ~14 | `lookup` |
| Cross-doc finds | 2 | `find` |
| `lookup`-negative | 1 | `negative` |
| `find`-negative | 1 | `find` (count=0) |
| (within the lookup pool) French-language items | 2–3 | `lookup` |
| **Total** | **~20** | |

All 9 doc types covered. Bilingual coverage in 10–15% of items. Negatives in both shapes.

## 8. Ground-truth shape

Same `GTItem` schema as PR-A — no schema changes:
- `lookup`: `answer_key = str` (canonical fact from the sidecar).
- `find`: `answer_key = {count, all, sample}` (list of gold stems from the cross-doc filter).
- `negative`: `lookup`-negative `answer_key = None`; `find`-negative `{count: 0, all: [], sample: []}`.

`gold_doc` is the filename stem (matches HC's `doc_title` format — same boundary discipline as PR-A).

## 9. Harness integration

Zero CLI changes. Existing sweep command works:

```bash
HC_API_KEY=<synthetic-scoped> ANTHROPIC_API_KEY=<...> \
  uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id answer-eval --mode answer-eval \
  --corpora synthetic --models claude-sonnet-4-6 --label synthetic-phase2b \
  --workdir "$WD" --api-base http://localhost:8100
```

Routes to `<workdir>/answer-eval/{captures,verdicts}/synthetic/<model>/...`.

## 10. Testing

- `test_generate_synthetic.py` — fixture with one synthetic doc per doc-type (sidecar + rendered text) in `tmp_path`, run generator, assert each recipe emits items with correct schema, correct sidecar→answer_key mappings, and the negative-guard fires when expected. ~10 tests.
- No `test_answer_judge.py` or `test_answer_eval.py` changes (judge + runner unchanged).
- Operational (post-merge): Task-8-equivalent live run with the synthetic-scoped key (already in hand). Compare numbers against CUAD + Enron baselines.

## 11. Out of scope (deferred to phase 3)

- **Aggregation questions** that exploit synthetic-being-fully-known ("Which vendor appears on the most invoices?", "What's the total Q2 revenue across all quarterly reports?", "Who is the most common signing manager on onboarding letters?"). These need a different judge shape and are explicitly held for phase 3 alongside `research`-type synthesis questions across all three corpora. Worth coming back to — synthetic's canonical structure makes aggregation ground truth uniquely cheap.
- **OpenAI / multi-model evaluation** — PR-C.
- **MCP system-prompt tuning** for the negative-hedging behavior surfaced in PR-A and confirmed in PR-A's Enron run — separate experiment after PR-C, evaluated against all 3 corpora and 2 models.

## 12. Open questions / decisions deferred to the plan

- **Exact recipe content per doc-type** — each recipe's templates and which sidecar fields to lift. Settled in the plan with full code; the human curation pass on the generated `synthetic.yaml` (Task 6 of the implementation plan) catches any awkward phrasing.
- **Second cross-doc `find`** — `_recipe_find_invoices_over_5000` is concrete; the second find item is TBD during implementation, picked from the doc-type whose sidecar structure best supports a clean filter (e.g. `board_minutes.decisions` mentions of a specific topic, or `onboarding_letter.start_date` in a specific quarter).
- **FR item picking** — the generator either scans for `lang: "fr"` / `languages_used` markers, or hardcodes a sidecar-id-to-language map. Settled during implementation based on what the actual sidecars carry.
