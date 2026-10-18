# The CUAD key's universe is 80: stored keyed-CUAD runs rescored, 2026-10-18

- Change: #815. `corpora/cuad_key.py` now matches an ingested contract to its `master_clauses.csv` row by the file
  name's identifiers up to the EDGAR accession number when the names differ, and the key was rebuilt from it.
- Method: offline. Every stored run's list answers scored with `runner/answer_key.py` as it is on `main` at
  `e1401ff`, once against the committed key before this change and once against the rebuilt one; no model call,
  no cloud spend, results directory read only. Cloud subjects are scored from their phase-1 baselines as
  `report.py`'s Cloud subject table does (document ids resolved through the baseline's cited ids and titles), local
  models from their phase-4 responses as `sweep.key_score_cell` does, for units whose last metrics row is done or
  degraded. The old-key rescore of every local unit equals the `answer_key_score` its `metrics.csv` recorded.
- Workdir: `~/Library/Application Support/Harbor Clerk/test-corpora/` on ix.

## What changed in the key

`HarpoonTherapeuticsInc_20200312_10-K_EX-10.18_12051356_EX-10.18_Development Agreement_Option Agreement` is in the
80-contract sample, but CUAD's CSV names its PDF `..._Development Agreement.PDF`, so the old exact match left it out
and every list was scored over 79. It is now matched by `HarpoonTherapeuticsInc_20200312_10-K_EX-10.18_12051356`,
which no other row or release PDF carries. No other contract is matched inexactly: run over all 510 PDFs of the
release, the same rule matches 510 of 510, Harpoon the only inexact one.

Every list's universe is now 80. Harpoon's row adds it to the positives of four of the six list questions:

| question | positives before | after |
|---|---|---|
| key-15 customer non-solicitation | 8 | 8 (Harpoon labelled No) |
| key-16 non-disparagement | 9 | 9 (No) |
| key-17 irrevocable or perpetual licence | 10 | 11 |
| key-18 right of first refusal, offer or negotiation | 16 | 17 |
| key-19 insurance | 21 | 22 |
| key-20 Delaware governing law | 7 | 8 |

The fact and date questions (key-1 to key-14) are unchanged: Harpoon displaces no contract already chosen for a
jurisdiction or a date.

## Before and after, per list question (F1)

One value where the score did not move; `before → after` where it did. A subject that named Harpoon gains a hit
where it is labelled (or an extra where it is not); one that did not loses recall where it is labelled.

| run | model | 15 | 16 | 17 | 18 | 19 | 20 | list mean |
|---|---|---|---|---|---|---|---|---|
| `rubric-20260921-1754` | `claude-sonnet-4-6` (cloud) | 0.40 | 0.56 | 1.00 | 0.640 → 0.615 | 0.811 → 0.821 | 0.923 → 0.933 | 0.722 → 0.721 |
| `rubric-20260921-1754` | `gemma4-26b-a4b` | 0.20 | 0.50 | 0.462 → 0.429 | 0.111 → 0.105 | 0.600 → 0.581 | 0.923 → 0.933 | 0.466 → 0.458 |
| `rubric-20260921-1754` | `gpt-oss-20b` | 0.22 | 0.18 | 0.889 → 0.842 | 0.435 → 0.417 | 0.645 → 0.667 | 0.600 → 0.636 | 0.495 → 0.494 |
| `rubric-20260921-1754` | `qwen3-8b` | 0.40 | 0.00 | 0.889 → 0.842 | 0.300 → 0.286 | 0.320 → 0.370 | 0.857 → 0.875 | 0.461 → 0.462 |
| `rubric-20260921-1754` | `qwen36-35b-a3b` | 0.62 | 0.57 | 0.706 → 0.667 | 0.00 | 0.743 → 0.757 | 0.250 → 0.222 | 0.481 → 0.472 |
| `bench-20260924-0724-keyed` | `claude-sonnet-4-6` (cloud) | 0.43 | 0.46 | 1.000 → 0.952 | 0.640 → 0.667 | 0.765 → 0.778 | 1.00 | 0.716 → 0.714 |
| `bench-20260924-0724-keyed` | `gemma4-26b-a4b` | 0.20 | 0.43 | 0.571 → 0.533 | 0.400 → 0.381 | 0.250 → 0.240 | 0.923 → 0.933 | 0.462 → 0.453 |
| `bench-20260924-0724-keyed` | `gpt-oss-20b` | 0.33 | 0.33 | 0.857 → 0.818 | 0.560 → 0.538 | 0.706 → 0.722 | 0.923 → 0.933 | 0.619 → 0.613 |
| `bench-20260924-0724-keyed` | `qwen3-8b` | 0.50 | 0.00 | 0.889 → 0.842 | 0.381 → 0.364 | 0.320 → 0.370 | 0.857 → 0.875 | 0.491 → 0.492 |
| `bench-20260924-0724-keyed` | `qwen36-35b-a3b` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.000 |
| `bench-20260924-2233-keyed-gemma` | `claude-sonnet-4-6` (cloud) | 0.43 | 0.46 | 1.000 → 0.952 | 0.640 → 0.667 | 0.765 → 0.778 | 1.00 | 0.716 → 0.714 |
| `bench-20260924-2233-keyed-gemma` | `gemma4-26b-a4b` | 0.50 | 0.00 | 0.571 → 0.533 | 0.308 → 0.302 | 0.500 → 0.512 | 0.923 → 0.933 | 0.467 → 0.463 |
| `bench-20260928-1050-keyed` | `claude-sonnet-4-6` (cloud) | 0.43 | 0.67 | 0.952 → 0.957 | 0.583 → 0.615 | 0.923 → 0.927 | 1.00 | 0.759 → 0.766 |
| `bench-20260928-1050-keyed` | `gemma4-12b` | 0.27 | 0.19 | 0.667 → 0.625 | 0.364 → 0.348 | 0.250 → 0.240 | 0.727 → 0.769 | 0.411 → 0.407 |
| `bench-20260928-1050-keyed` | `gemma4-26b-a4b` | 0.50 | 0.154 → 0.150 | 0.667 → 0.690 | 0.462 → 0.450 | 0.320 → 0.370 | 0.857 → 0.875 | 0.493 → 0.506 |
| `bench-20260928-1050-keyed` | `gpt-oss-20b` | 0.18 | 0.33 | 0.889 → 0.842 | 0.533 → 0.516 | 0.688 → 0.706 | 0.857 → 0.875 | 0.580 → 0.576 |
| `bench-20260928-1050-keyed` | `qwen3-8b` | 0.324 → 0.316 | 0.00 | 0.889 → 0.842 | 0.300 → 0.286 | 0.00 | 0.00 | 0.252 → 0.241 |
| `bench-20260928-1050-keyed` | `qwen35-4b` | 0.46 | 0.27 | 0.500 → 0.471 | 0.476 → 0.455 | 0.320 → 0.370 | 0.923 → 0.933 | 0.491 → 0.493 |
| `bench-20260928-1050-keyed` | `qwen35-9b` | 0.20 | 0.57 | 0.947 → 0.900 | 0.593 → 0.571 | 0.444 → 0.483 | 0.387 → 0.424 | 0.524 → 0.525 |
| `bench-20260928-1050-keyed` | `qwen36-35b-a3b` | 0.33 | 0.00 | 0.889 → 0.842 | 0.00 | 0.444 → 0.483 | 0.923 → 0.933 | 0.432 → 0.432 |
| `bench-20260928-1050-keyed` | `qwen38-27b` | 0.46 | 0.00 | 0.667 → 0.625 | 0.476 → 0.455 | 0.320 → 0.308 | 0.923 → 0.933 | 0.475 → 0.464 |
| `bench-20261014-2328-cuad-fable` | `claude-fable-5-1` (cloud) | 0.62 | 0.67 | 1.00 | 0.667 → 0.640 | 1.00 | 1.00 | 0.825 → 0.820 |
| `bench-20261015-0011-cuad-astra` | `gpt-6-astra` (cloud) | 0.82 | 0.67 | 1.00 | 0.741 → 0.759 | 0.977 → 0.978 | 1.00 | 0.868 → 0.871 |
| `bench-20261015-2259-cuad-fable-rerun` | `claude-fable-5-1` (cloud) | 0.80 | 0.43 | 1.00 | 0.828 → 0.800 | 1.00 | 1.00 | 0.843 → 0.838 |

`bench-20260924-2233-keyed-gemma` reused `bench-20260924-0724-keyed`'s baselines (the files are identical), so its
Sonnet row is the same answers.

## Reading

- **Every list mean moves by 0.013 or less.** In the grid of the 2026-10-15 Fable report, one figure changes at two
  places: Sonnet 4.6 (2026-09-28) goes from 0.76 to 0.77 on lists. Fable stays 0.82 on lists and 0.95 over 20,
  astra 0.87 and 0.96, `gpt-oss-20b` (2026-09-28) 0.58 on lists.
- **Orderings change only between scores that were tied or within 0.04.** On key-18 the 2026-09-24 Sonnet baseline
  (0.640 → 0.667) now edges Fable's first run (0.667 → 0.640), and within `bench-20260928-1050-keyed` Sonnet
  (0.583 → 0.615) passes `qwen35-9b` (0.593 → 0.571); on list means, `rubric-20260921-1754`'s `qwen3-8b`
  (0.461 → 0.462) passes its `gemma4-26b-a4b` (0.466 → 0.458). Gaps that small were not a ranking before either.
- **The cloud subjects named Harpoon where they judged its text supports it.** Both Fable runs scored 1.00 on key-17,
  key-19 and key-20 and still do, with Harpoon counted as a hit on each; astra likewise on key-17 and key-20, and
  it gains Harpoon on key-19 (0.977 → 0.978). On key-18 neither Fable run claims it: both put Harpoon–AbbVie under
  headings the scorer reads as rejected (the first run under "Mentions / borderline" and "Reviewed and excluded",
  the rerun under "Candidates reviewed and excluded"), calling AbbVie's §5.9.2 "first right ... to negotiate" for
  third-party licences an IP-control provision, so the new positive is a miss and Fable's key-18 falls by 0.03.
  astra lists it in its own "flagged separately" table, which the scorer counts, and gains it. Whether §5.9.2 is a
  right of first negotiation is the annotators' call against the models'; the key keeps the annotators'.
- **Local models mostly lose a little on key-17 and key-18 and gain on key-19 and key-20.** They rarely name Harpoon
  for the licence or first-refusal questions, so they lose recall there, and often name it for insurance and
  Delaware law, so they gain a hit there.

## Not verified

The rescore reads stored answers only; nobody re-read the Harpoon clauses behind key-17, key-19 and key-20 for this
note (#815 cites 8, 16 and 2 keyword hits in its text). Phase-4 scores in each run's `metrics.csv` and the reports
rendered from it are left as recorded, over 79.

## Addendum: under the scorer after #840

The figures above use the scorer at `e1401ff`. #840 (merged 2026-10-18, before this change) counts hedged sections such as
"Mentions / borderline" as listed. Rescored with the current scorer, the three cloud runs move as follows between the old
key and the new one:

| run | changed questions | lists, old key → new key |
|---|---|---|
| bench-20261014-2328-cuad-fable | key-18 0.667 → 0.690 | 0.825 → 0.829 |
| bench-20261015-0011-cuad-astra | key-18 0.741 → 0.759; key-19 0.977 → 0.978 | 0.862 → 0.865 |
| bench-20261015-2259-cuad-fable-rerun | key-18 0.828 → 0.800; key-19 0.977 → 0.978 | 0.873 → 0.868 |

The first Fable run now gains Harpoon on key-18, because its "Mentions / borderline" section counts. The rerun still loses
it, because its "Reviewed and excluded" section still rejects. The local runs are not affected by #840.
