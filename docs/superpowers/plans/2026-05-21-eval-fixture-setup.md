# Eval-Fixture Setup — Implementation Plan

> Executes the design in `docs/superpowers/specs/2026-05-21-retrieval-eval-corpus-design.md`. Establishes the frozen `eval-fixture-v1` corpus + baselines that gate every phase of the retrieval/MCP upgrade.

**Goal:** A retrieval-eval gate that produces a real number — a unified CUAD + Enron-500 + synthetic corpus loaded once into `lka`, with title-bearing baselines captured against it.

**Tech:** existing `scripts/test_corpora` sweep harness + the `--mode retrieval-eval` path (PR #372); one small new sweep flag.

---

## Context — the wrinkle

The spec assumed baseline capture against a unified corpus is straightforward. It isn't: the sweep's phase-1 per-unit loop ingests **one corpus at a time**, calling `_ingest_corpus` (which does `delete_all_documents` + re-ingest) on every corpus change. Run phase 1 over three corpora and `lka` ends holding only the last one — exactly the churn the spec is trying to escape.

Fix: a `--no-ingest` flag (Task 1). With the unified corpus pre-loaded, `sweep --phases 1 --no-ingest` baselines every question against the already-loaded `lka` without touching it.

**Enron cap:** no code needed — `enron.acquire()` already defaults `random_count` to 500 (`RANDOM_COUNT_DEFAULT = 500` in `corpora/enron.py`). The 10k+ in `lka` now is a separate, larger ingestion artifact; the fixture acquire uses the default.

---

## Task 1 — `--no-ingest` flag on the sweep  *(code — Claude drives)*

**Files:** `scripts/test_corpora/runner/sweep.py`; `scripts/test_corpora/tests/test_sweep_ingest.py` (or a new test).

- [ ] Add `--no-ingest` (argparse `store_true`) to `make_parser()`, help: "skip all corpus ingest; baseline/run against whatever is already loaded in HC. For eval-fixture capture against a pre-loaded unified corpus."
- [ ] In the per-unit loop, guard the ingest block: the `if phase in (1, 4, 5) and u.corpus != current_corpus_in_db:` ingest path must be skipped entirely when `args.no_ingest` is set. When skipped, set `current_corpus_in_db = u.corpus` anyway (so the loop's bookkeeping stays consistent) but call neither `_ingest_corpus` nor `_can_skip_ingest`.
- [ ] Test: with `--no-ingest`, `_ingest_corpus` is never called for a multi-corpus phase-1 plan. Use the existing test patterns in `test_sweep_ingest.py` (mock `HarborClerkClient`).
- [ ] `uv run ruff check/format`; run `scripts/test_corpora/tests/test_sweep_ingest.py` + `test_plan_units.py`.
- [ ] Commit: `feat(test-corpora): --no-ingest flag for baseline capture against a pre-loaded corpus`.

## Task 2 — Build + ingest the unified eval corpus  *(operational — DESTRUCTIVE — operator)*

**⚠️ Destructive — `lka` snapshot must exist first** (`~/lka-snapshot-2026-05-21.dump`, done).

- [ ] Acquire the three corpora to disk: `sweep --phases 0 --run-id eval-fixture-v1 --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora"`. Produces `cuad/`, `enron/`, `synthetic/` ingest dirs (Enron = custodian + 500 random).
- [ ] Clear `lka`: delete all existing watched folders + documents (via the Folders UI, or `DELETE`/`watch_folder_delete` API). This drops the 10k Enron artifact.
- [ ] Add **three watched folders** — one per corpus ingest dir (cuad, enron, synthetic). HC ingests all three → `lka` is now the unified eval corpus.
- [ ] Wait for the pipeline to drain (Observatory / queue tray idle). Record the total doc count.

## Task 3 — Capture `eval-fixture-v1` baselines  *(operational — ~$5 Sonnet — operator)*

Requires Task 1 merged/available and Task 2's corpus loaded.

- [ ] `HC_USERNAME=… HC_PASSWORD=… ANTHROPIC_API_KEY=… sweep --phases 1 --no-ingest --run-id eval-fixture-v1 --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora" --api-base http://localhost:8100`
- [ ] This runs Sonnet + HC MCP over every question (cuad + enron + synthetic banks) against the pre-loaded unified `lka`, writing `results/eval-fixture-v1/baselines/<corpus>/<q>.json` — with `cited_doc_titles` (post-#367 harness).
- [ ] Spot-check a baseline JSON has a non-empty `cited_doc_titles`. **Freeze these — do not regenerate** unless the corpus changes.

## Task 4 — Run the embedding-v2 reference eval  *(operational — Claude can drive with creds)*

- [ ] `HC_USERNAME=… HC_PASSWORD=… sweep --mode retrieval-eval --run-id eval-fixture-v1 --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora" --api-base http://localhost:8100 --label embedding-v2`
- [ ] Read `results/eval-fixture-v1/retrieval-eval/embedding-v2/summary.json` → `overall.recall_at_k["10"]`. Absolute number (no `--prior-label` — see spec §6). Against frontier gold citations: ≥0.8 strong, 0.6–0.8 acceptable, below → investigate.
- [ ] This `embedding-v2` label is the **reference point** — phase 1 (late chunking) will diff `--prior-label embedding-v2`.

## Task 5 — Amend the master plan  *(docs — Claude drives)*

**File:** `docs/superpowers/plans/2026-05-17-retrieval-mcp-upgrade-master-plan.md`.

- [ ] Replace the master plan's hand-wave at "the sweep baselines" in its Retrieval-Eval Gates section with the concrete per-phase runbook: each phase runs `sweep --mode retrieval-eval --run-id eval-fixture-v1 --label phase-N --prior-label phase-(N-1)`.
- [ ] Add a guard-rail note: the model-comparison sweep (8-local-model matrix) must not run on the eval box during the upgrade — it re-ingests and forces a re-baseline.
- [ ] Commit with the docs change.

---

## Sequencing & ownership

| Task | Owner | Blocking dependency |
|---|---|---|
| 1 — `--no-ingest` flag | Claude (subagent) | none |
| 2 — ingest unified corpus | **Operator** (destructive) | snapshot done ✓ |
| 3 — capture baselines | Operator (needs API key) | Tasks 1 + 2 |
| 4 — reference eval | Operator, or Claude with creds | Task 3 |
| 5 — amend master plan | Claude | none |

Tasks 1 and 5 are non-destructive and have no dependency on the box state — Claude does them now. Tasks 2-4 are the operator's sequential runbook once Task 1 lands.
