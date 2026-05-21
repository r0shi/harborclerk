# Retrieval-Eval Gate — Stable Corpus & Baseline Strategy

**Status:** Design / pre-plan
**Date:** 2026-05-21
**Corrects:** an under-specification in `2026-05-17-embedding-v2-design.md` and `2026-05-17-retrieval-mcp-upgrade-master-plan.md` — both assumed "the sweep baselines" could gate retrieval changes directly. They can't. See Problem.

## Problem

The 6-phase retrieval/MCP upgrade gates each phase with `sweep --mode retrieval-eval`: replay baseline questions through `/api/search`, score recall@K / MRR / nDCG against the documents a frontier model (Sonnet, via MCP) cited when it answered the same questions. For that score to mean anything, the gate must compare against a corpus whose document identities line up with the baselines.

The first real run — embedding-v2, 2026-05-21 — returned **0.000 on every question across every metric**. That is a broken comparison, not a retrieval result. Two compounding causes:

1. **UUID churn.** The `2026-05-05-prod` baselines store `cited_doc_ids` — raw document UUIDs. The model-comparison sweep does `delete_all_documents` + re-ingest between phases, and every re-ingest mints fresh UUIDs. Baseline UUIDs captured in phase 1 never match a corpus re-ingested in phase 4: same documents, disjoint identifiers.
2. **Single-corpus DB.** Harbor Clerk holds one corpus at a time. After the sweep's phase-4 Enron run, `lka` contained only Enron — so the CUAD and synthetic baseline questions had no corpus to hit at all; `/api/search` returned unrelated Enron docs.

Title-based matching (already shipped — `58b5c04`, `fix(retrieval-eval): match documents by title, not UUID`) fixes cause #1's *matching layer*: document titles (filename stems) are stable across re-ingestion, so the harness now compares normalized titles, not UUIDs. But that fix only helps if the baselines actually carry `cited_doc_titles` (only baselines generated after PR #367 do — the `2026-05-05-prod` set predates it and is unusable for the gate) **and** the corpus under test is actually loaded. The gate still needs a deliberate, frozen fixture. This spec defines it.

## Goal

A dedicated, frozen retrieval-eval fixture — a fixed corpus plus baselines captured against it — established once and reused to gate every phase of the 6-phase upgrade. Robust to re-ingestion; independent of the model-comparison sweep's churn.

## Design

### 1. The eval corpus is the box's corpus — freeze it

Harbor Clerk is single-tenant: one HC instance talks to one database. There is no clean way to have "two corpora loaded." The options:

| Option | Mechanics | Verdict |
|---|---|---|
| **A. Freeze the working DB** | Ingest the eval corpus into `lka` once; capture baselines; never re-ingest while the upgrade is in progress. | **Recommended.** Zero new infra. Matches the "box dedicated to this work" reality. |
| B. Dedicated eval DB | A second database (`harbor_clerk_eval`) + a second HC config (different DB name, ports) pointed at it. | More infra; only worth it if the model-comparison sweep must run concurrently on the same box. |
| C. Snapshot/restore | Keep a `pg_dump` of the eval-corpus DB; restore before each gate run. | Viable fallback; restore is slow at corpus scale. Use only if the box must be shared. |

**Recommendation: Option A.** The box is dedicated to the retrieval upgrade. Its `lka` corpus *is* the eval corpus. The model-comparison sweep (the 8-local-model matrix, which re-ingests per phase) is a different activity — do not run it on this box during the upgrade. If you must, that is a separate box, or you re-establish the eval fixture afterward.

The key discipline this buys: **between establishing the fixture and finishing the 6-phase upgrade, the eval corpus is never re-ingested.** Document identities stay put. (Title matching means a re-ingest wouldn't *break* the gate, but freezing keeps recall numbers comparable phase-to-phase without re-baselining.)

### 2. The eval corpus content

Reuse the existing `scripts/test_corpora` corpora — CUAD (legal/contract PDFs, some OCR), an Enron email subset, and the synthetic bilingual EN/FR corpus — because the question banks in `scripts/test_corpora/questions/*.yaml` are already written against them. The corpus and its questions are coupled; don't decouple them.

Size: the Enron subset should be bounded (hundreds, not the full 10k+) — large enough that recall@10 is statistically meaningful, small enough that one ingest and one baseline-capture pass are cheap. The existing corpora manifests already define bounded sets; use them as-is unless a sweep has inflated the Enron count.

### 3. Baseline capture

Capture baselines once, against the frozen corpus, with a **post-PR-#367 harness** so `cited_doc_titles` is recorded alongside `cited_doc_ids`:

```
sweep --phases 1 --run-id eval-fixture-v1 --workdir <eval workdir> --api-base http://localhost:8100
```

Phase 1 runs Sonnet + the HC MCP server over each question, recording the documents it cited. Cost is modest (~$5, ~20 min per the sweep runbook). The output — `results/eval-fixture-v1/baselines/<corpus>/<q>.json` — is the frozen reference. **Do not regenerate it** unless the corpus itself changes.

Use a dedicated `--run-id` (`eval-fixture-v1`) distinct from any model-comparison sweep run-id, so the two never collide in the results tree.

### 4. Title-based matching — done

`58b5c04` already changed `retrieval_eval.py` to load `cited_doc_titles`, fetch `doc_title` from `/api/search`, normalize (strip + casefold + dedup, empties dropped), and skip legacy UUID-only baselines with a warning. No further harness work is needed for matching. `metrics.py` is unchanged (recall/MRR/nDCG are generic string-set functions).

### 5. Per-phase gating

For each phase N of the upgrade (N = embedding-v2, late-chunking, metadata, graph, propositions, ...):

1. Deploy phase N's code + schema to the box; let any re-embed/re-process finish.
2. `sweep --mode retrieval-eval --run-id eval-fixture-v1 --label phase-N --prior-label phase-(N-1)`
3. Read `vs_phase-(N-1).json` — per-question deltas, regressions, improvements. Read `summary.json` — overall recall@10.
4. Gate: the phase's design states its pass threshold (e.g. embedding-v2 wanted +0.08 recall@10).

All phases share the one `eval-fixture-v1` baseline set. Each phase's run is a label; consecutive labels diff cleanly.

### 6. The lost e5-small reference

The embedding-v2 design's gate ("+0.08 recall@10 vs the e5-small baseline") assumed a retrieval-eval run existed for the *pre-upgrade* (e5-small) stack. None does — the box was cut over to Granite-R2 before retrieval-eval was ever run. That delta is unrecoverable without reverting.

**Going forward, this must not recur:** capture a retrieval-eval run *before* each phase's cutover, so the phase has a "prior" to diff against. The first such reference is established by the embedding-v2 recovery below.

## Immediate recovery for embedding-v2

Since the e5-small reference is gone, embedding-v2 gets an **absolute** measurement, not a delta:

1. Confirm `lka` is stable (no sweep mid-re-ingest) and the Granite-R2 re-embed is 100% complete.
2. Capture fresh baselines against the *current* embedding-v2 corpus, with the post-#367 harness:
   `sweep --phases 1 --run-id eval-fixture-v1 --workdir <eval workdir> --api-base http://localhost:8100`
3. `sweep --mode retrieval-eval --run-id eval-fixture-v1 --label embedding-v2` — no `--prior-label`.
4. Read `summary.json` → `overall.recall_at_k["10"]`. Interpret as an absolute: against frontier-model gold citations, recall@10 ≥ 0.8 is strong; 0.6–0.8 is acceptable; below that, investigate.
5. This `embedding-v2` run becomes the **reference point** — phase 1 (late chunking) runs `--prior-label embedding-v2` and the delta becomes measurable again from here on.

Caveat: with `lka` currently holding only the Enron corpus (a sweep artifact), this recovery measures Enron retrieval only. For the full CUAD + Enron + synthetic picture, the box's corpus must first be set to the full eval corpus (Setup task 1).

## Setup tasks

1. **Establish the eval corpus.** Ingest the full CUAD + Enron-subset + synthetic corpora into `lka` (via watched folders or the sweep's phase-0/ingest path). This becomes the frozen fixture. Record the doc count.
2. **Capture `eval-fixture-v1` baselines.** Sweep phase 1, post-#367 harness, dedicated run-id. Freeze.
3. **Run the embedding-v2 reference eval.** Per "Immediate recovery" above. Record the absolute recall@10.
4. **Document the per-phase gate runbook** — the three commands in §5 — in the master plan, replacing its current hand-wave at "the sweep baselines."
5. **Guard rail:** add a note to the master plan that the model-comparison sweep must not run on the eval box during the upgrade (it re-ingests and would force a re-baseline).

## Open questions

- **Enron subset size** — if the existing manifest's Enron count is large (the box currently has 10k+), bound it to a few hundred for cheaper ingest + baseline capture. Decide the cap.
- **Where the eval HC runs** — Option A assumes the upgrade box. If the model-comparison sweep needs to run in parallel, revisit Option B (dedicated DB + second HC config).
- **Baseline judge model** — phase 1 uses Sonnet. If a newer frontier model is materially better at the gold-citation task, consider it — but keep the judge fixed across all 6 phases so the baselines stay comparable.

## Cross-references

- Harness fix: `58b5c04` (`fix(retrieval-eval): match documents by title, not UUID`).
- Gate harness: `scripts/test_corpora/runner/retrieval_eval.py` (PR #372).
- Embedding-v2 design (the gate's origin): `docs/superpowers/specs/2026-05-17-embedding-v2-design.md`.
- Master plan (to be amended per Setup task 4): `docs/superpowers/plans/2026-05-17-retrieval-mcp-upgrade-master-plan.md`.
