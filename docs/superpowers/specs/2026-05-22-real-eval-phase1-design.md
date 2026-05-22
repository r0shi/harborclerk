# Real Answer-Level Eval — Phase 1 Design (CUAD)

**Status:** Approved design. Ready for an implementation plan.
**Date:** 2026-05-22
**Companion docs:**
- `2026-05-22-real-eval-design-sketch.md` — the overall direction and why the current eval is insufficient.
- `2026-05-21-retrieval-eval-corpus-design.md` — the current interim retrieval-eval.

---

## 1. Goal

Validate the product claim — *a frontier model, via Harbor Clerk's MCP server, can
reliably produce complete, correct, grounded answers from the corpus* — with an
eval that measures **answer quality against external ground truth**, not the
circular "score retrieval against what the model itself cited."

Phase 1 builds the foundation and proves it end-to-end on one corpus.

## 2. Phase 1 scope

- **Corpus:** CUAD only (legal contracts). Enron and synthetic are later phases.
- **Question type:** clause-extraction lookups — the most checkable kind, and
  exactly what a law office asks. Synthesis/stats question types are later.
- **Model under test:** Sonnet (one frontier model). Multi-model is later — but
  the storage layout is model-keyed from day one (see §7).
- **Deliverable:** a repeatable `answer-eval` that runs a model through HC's MCP
  over a ground-truth set and reports per-question correctness, groundedness,
  and completeness.

## 3. Why CUAD

CUAD's release (`CUAD_v1.zip`, already downloaded by `corpora/cuad.py`) ships
`master_clauses.csv` — Atticus Project lawyers labeled 41 clause categories per
contract with the exact answer spans. For the ~80 contracts our `cuad.py`
samples, that is **expert-verified ground truth, free**. `cuad.py` currently
uses only the PDFs and ignores the annotations.

## 4. Core principle — ground truth is independent of the MCP

Ground truth must be established **without** going through HC's MCP or its
retrieval index — the MCP is the system under test, so truth coupled to it is
worthless. For CUAD this is automatic: the answer keys come from
`master_clauses.csv`, read straight from the dataset.

This generalizes to later phases: for lookup/recall/stats questions on Enron
and synthetic, derive truth by directly querying the **raw filesystem** (grep
the source documents → the *exhaustive* relevant-doc set, which also makes
"thoroughness" measurable) or **structured DB facts** (e.g. `entities` counts)
— never by reusing HC's FTS/pgvector index. It does **not** apply to synthesis
questions; those still need human/expert judgment.

## 5. Architecture — components

```
CUAD release (master_clauses.csv)
        │  generator (explicit, one-shot)
        ▼
groundtruth/cuad.yaml          ← frozen, version-controlled
        │  answer-eval runner  ← model + HC MCP via corpus-scoped key
        ▼
answer-eval/captures/cuad/<model>/<qid>.json   ← model-keyed, durable, reused
        │  judge (LLM-as-judge)
        ▼
answer-eval/verdicts/cuad/<model>/<qid>.json   ← model-keyed, durable, reused
        │  report writer
        ▼
summary.json + per-question detail
```

Six units, each with one purpose:

1. **Ground-truth generator** — reads `master_clauses.csv`, emits `cuad.yaml`.
2. **Ground-truth set** — the frozen YAML artifact.
3. **Eval bed** — operational: three watched folders + three folder-scoped keys.
4. **Answer-eval runner** — runs a model via MCP, persists captures + transcripts.
5. **Judge/scorer** — scores a captured answer against the ground truth.
6. **Report writer** — aggregates verdicts into `summary.json`.

## 6. The eval bed — all corpora resident, no re-ingest

Folder-scoped API keys exist: `ApiKey.scope_folder_ids` (migration 0013), and
the MCP server consumes them (`mcp_server.py:104`). So:

- Ingest **CUAD + Enron + synthetic** as three watched folders, **additively** —
  no `delete_all_documents`, no snapshot/restore. Existing Enron stays.
- Mint **one read-only, folder-scoped API key per corpus**.
- Each eval run uses its corpus's scoped key; HC returns only that corpus.
- Later Enron/synthetic phases need **zero re-ingest**.

**Verification task (early, blocking):** confirm `scope_folder_ids` filters
*before* ranking — a scoped search over the 3-corpus index must return the same
results a single-corpus index would. **Fallback:** if it filters post-ranking,
phase 1 reverts to single-corpus loads (load CUAD alone) and the multi-corpus
bed is deferred to its own task.

## 7. Artifacts, persistence, and regeneration discipline

Two kinds of durable artifact. **Eval runs reuse both by default; regeneration
is always an explicit, narrow switch — never a side-effect of running the eval.**

- **Ground-truth set** — `scripts/test_corpora/groundtruth/cuad.yaml`,
  version-controlled in the repo. Regenerated only by explicitly running the
  generator. Frozen between regenerations.
- **Answer captures & judge verdicts** — under a stable workdir path
  `answer-eval/{captures,verdicts}/<corpus>/<model>/<qid>.json` (NOT
  run-id-scoped, so they carry over). **Keyed by model** — adding ChatGPT writes
  alongside Sonnet, never over it. An eval run reuses any capture/verdict
  already present; `--refresh` re-runs the model, `--rejudge` re-runs the judge,
  `--rerun model=<m>` scopes either to one model.

## 8. The ground-truth set

`cuad.yaml` — ~12–15 items:

```yaml
corpus: cuad
items:
  - id: cuad-gt-1
    question: "What is the governing law of the <contract> agreement?"
    clause_category: "Governing Law"
    gold_doc: "<filename stem of the contract PDF>"
    answer_key: "<verbatim labeled span from master_clauses.csv>"
    type: lookup
  - id: cuad-gt-14
    question: "Does the <contract> agreement contain a most-favored-nation clause?"
    clause_category: "Most Favored Nation"
    gold_doc: "<filename stem>"
    answer_key: null            # CUAD labeled this category absent
    type: negative
```

- Categories chosen for crisp answers: Governing Law, Parties, Agreement Date,
  Expiration Date, Termination for Convenience, Non-Compete, Cap on Liability,
  Exclusivity, Most Favored Nation.
- **~3 negatives** — (contract, category) pairs CUAD labeled absent.
- `gold_doc` must be one of the ~80 contracts `cuad.py` samples; the generator
  filters `master_clauses.csv` to that sampled set.
- `answer_key` is the verbatim CUAD span; the judge checks the model's answer is
  *consistent with* it, not byte-equal.

**Generator:** `scripts/test_corpora/groundtruth/generate_cuad.py` — explicit
one-shot. Reads the extracted CUAD release, selects items, writes `cuad.yaml`.
The output is eyeballed once by a human and committed.

## 9. The eval run

Per ground-truth item, the answer-eval runner:
1. Runs the model under test through HC's MCP — reusing `claude_baseline.py`'s
   tool-call loop — authenticated with the **corpus-scoped API key**.
2. Captures `{answer, cited_doc_ids, cited_doc_titles, tool_call_count,
   tool_transcript, model, timestamp}`.
3. **New:** persists the full `tool_transcript` (each call: tool, args, result
   summary). `claude_baseline.py` currently saves only `tool_call_count`; this
   is a small additive change. Needed now for groundedness, later for tool-use
   auditing.

## 10. Scoring

An LLM judge receives `{question, model answer, model's cited docs + passages,
answer_key, gold_doc, type}` and emits three scores:

- **Correctness** — is the answer consistent with `answer_key`? For `negative`
  items, did it correctly say the clause is absent?
- **Groundedness** — is every factual claim cited, does each citation actually
  support its claim, and was the `gold_doc` among the cited docs?
- **Completeness** — does the answer cover what `answer_key` contains?

Judge model: **Sonnet**. Independence is not a concern in phase 1 — the judge
adjudicates *against an external expert label*; it is not itself the source of
truth. (Revisit when the eval grows to synthesis questions, where the judge
carries more weight.)

Verdicts are persisted per (model, question) per §7.

## 11. Harness integration

A new `--mode answer-eval` on the sweep, mirroring `--mode retrieval-eval`:

```
sweep --mode answer-eval --corpus cuad --model claude-sonnet-4-6 --label <name>
```

Loads `groundtruth/cuad.yaml` → for each item, reuse-or-run the capture →
reuse-or-run the judge → write the report. Reuses `claude_baseline.py`, the
phase-5 judge pattern, and the corpora/manifest plumbing. 529/overload
resilience is inherited from the harness fix landing on a separate branch.

## 12. De-risk probe (early task)

Before the CUAD set exists, point the judge at the 16 Enron baselines already
captured at `results/sanity-2026-05-22/baselines/enron/`. This validates the
judge harness end-to-end on existing data and gives a first read on whether
those answers carry consistent citations. Note: those baselines predate
transcript persistence and store no passage text, so the check here is partial
— citation *presence*, not full citation *support*. Full groundedness arrives
with the CUAD eval, which captures passages fresh (§9).

## 13. Output

Each run writes a **labeled report** — `answer-eval/reports/<label>/summary.json`
— so successive eval runs stay distinct while captures/verdicts (§7) are reused.
`summary.json`: mean correctness / groundedness / completeness — overall and
split by `type` (lookup vs negative) — per model. Plus per-question detail:
the answer, cited docs, the three scores, and the judge's rationale.

## 14. Testing

Unit tests under `scripts/test_corpora/tests/`, following existing patterns:
- generator: a mock `master_clauses.csv` → expected `cuad.yaml`.
- judge/scorer: mock judge responses → correct score parsing, incl. negatives.
- report aggregation: synthetic verdicts → expected `summary.json`.
- runner: reuse-vs-refresh logic (mock `claude_baseline`), model-keyed paths.

## 15. Out of scope (later phases)

Synthesis and stats question types; Enron and synthetic ground-truth sets;
multiple frontier models; variance/repeat runs; deep tool-use trajectory
auditing (transcripts are *persisted* in phase 1, *analyzed* later).

## 16. Open questions / decisions deferred to the plan

- Exact selection of the ~12–15 CUAD (contract, category) items — the generator
  picks deterministically; a human curates once.
- Judge output schema and prompt wording — settle during implementation.
- Whether `--mode answer-eval` is one flag with a `--corpus` arg, or per-corpus;
  one flag + `--corpus` is the assumption here.
