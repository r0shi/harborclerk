# Benchmark run `bench-20260929-1943-synthetic` on ix, 2026-09-30

- Suite commit: `4fbf724`
- Run started: 2026-09-29T23:43:19Z
- Corpora: synthetic
- Cloud spend: USD 7.79 of a 25.00 cap over 239 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **warn** at 2026-09-29T23:43:18Z, llama.cpp pin v0.4.1.
  - warn: swap in use: 3945 MB

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | forced | forced pass | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | `gemma4-12b` | 18 | 18 | 16 | 2 | 2 | 0 | 0 | 0.41 | 0.10 | 233 | 16 | 3 | 6 | 7 | 2.25 | 1.38 | n/a |
| synthetic | `gemma4-26b-a4b` | 18 | 18 | 13 | 5 | 5 | 1 | 0 | 0.48 | 0.11 | 271 | 13 | 2 | 7 | 4 | 2.62 | 1.85 | n/a |
| synthetic | `gpt-oss-20b` | 18 | 18 | 15 | 3 | 3 | 0 | 0 | 0.57 | 0.06 | 277 | 15 | 2 | 4 | 9 | 2.07 | 1.40 | n/a |
| synthetic | `qwen3-8b` | 18 | 18 | 17 | 1 | 1 | 0 | 0 | 0.36 | 0.08 | 95 | 17 | 2 | 3 | 12 | 1.29 | 0.94 | n/a |
| synthetic | `qwen35-4b` | 18 | 18 | 17 | 1 | 1 | 0 | 0 | 0.55 | 0.13 | 206 | 17 | 4 | 3 | 10 | 2.29 | 1.88 | n/a |
| synthetic | `qwen35-9b` | 18 | 18 | 14 | 4 | 4 | 2 | 0 | 0.54 | 0.13 | 222 | 14 | 3 | 4 | 7 | 1.86 | 1.43 | n/a |
| synthetic | `qwen36-35b-a3b` | 18 | 18 | 10 | 8 | 8 | 0 | 0 | 0.48 | 0.12 | 142 | 10 | 2 | 4 | 4 | 2.00 | 1.40 | n/a |
| synthetic | `qwen38-27b` | 18 | 18 | 7 | 11 | 11 | 3 | 0 | 0.55 | 0.12 | 836 | 7 | 1 | 2 | 4 | 1.57 | 1.29 | n/a |

**35 answers were forced by the app** (8 by context budget, 27 by time budget): it stopped the search and answered from what it had, saying so. They are judged (except the app's own sentences for a model that then produced nothing, a model server that failed, or a thought cut short, which have no answer to judge), counted under `forced` and `forced pass`, and included in `degraded`; `judged` and the columns after it are the answers the model chose to give.

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 18 | 96 | 132 | 950687 | 3432117 | 55450 | 7.5659 |
| judge | 143 | 143 | 439987 | 0 | 0 | 110995 | 0.2212 |

## Reading

**The synthetic corpus (280 generated business documents, the 2026-09-26 regeneration with named vendors, clients and campaigns), 16 questions of which two are asked in English and French (18 units), all eight registered models, one build: app `9a425ca`, harness `4fbf724`.** 144 model units and 18 baselines, no error, no restart, one launch, 15 h 17 min, USD 7.79; it started on its own when the Enron run exited. Preflight warned on swap (3.9 GB) and passed everything else. No answer key: every score is the judge against a Claude baseline with the same tools. The four incumbents have a row on this corpus from 2026-09-27 (build `6da31e0`, with #730 but before #742 and #755).

### The grid

| model | ask (11) | research (7) | forced | forced pass | median ask s | median research s | 09-27 (all 18) |
|---|---|---|---|---|---|---|---|
| `qwen35-9b` | 4 / 5 / 2 | 1 / 0 / 6 | 4 | 2 | 186 | 247 | new |
| `qwen35-4b` | 4 / 0 / 7 | 0 / 3 / 4 | 1 | 0 | 122 | 266 | new |
| `gemma4-26b-a4b` | 3 / 4 / 4 | 0 / 4 / 3 | 5 | 1 | 271 | 346 | 6 / 5 / 3 |
| `gemma4-12b` | 3 / 2 / 6 | 0 / 4 / 3 | 2 | 0 | 202 | 372 | new |
| `qwen38-27b` | 3 / 2 / 6 | 1 / 2 / 4 | 11 | 3 | 780 | 836 | new |
| `gpt-oss-20b` | 2 / 2 / 7 | 0 / 2 / 5 | 3 | 0 | 183 | 658 | 4 / 2 / 6, 3 errors |
| `qwen3-8b` | 2 / 2 / 7 | 0 / 1 / 6 | 1 | 0 | 88 | 550 | 2 / 3 / 13 |
| `qwen36-35b-a3b` at 16,384 | 1 / 4 / 5, 1 unjudged | 1 / 2 / 4 | 8 | 0 | 81 | 195 | 2 / 0 / 6, 10 forced |

### What the numbers support

- **This corpus is hard for every local model, and the judge's noise is as large as the gaps.** No model passes more than 4 of 11 asks or 1 of 7 research questions. Gemma 4 26B-A4B went from 6 passes on 2026-09-27 to 2 on the same corpus with no change to the model and a build that changed only what happens after a stop; the other three incumbents moved by at most two. A grid this flat ranks nothing.
- **The French forms lose every model.** `ask-10` is asked twice: in English five models pass it; in French none, seven fail. `research-6` in English fails for all eight, in French for seven with one marginal. The corpus is bilingual and retrieval is bilingual by design (`fts_en` and `fts_fr`), so a French question that finds nothing where its English twin finds the answer is a defect to locate, in the question, the retrieval or the models' French. Filed as an issue from this report.
- **Aggregates still fail.** `ask-5`, `ask-6`, `ask-7` and `ask-8` (counts and totals across the corpus) fail for all eight; `research-1` too. `documents_by_date` earned one pass on 2026-09-27; nothing repeats it here. `ask-9` passes for seven of eight and `ask-3` for three with five marginals: the lookups work.
- **Qwen3.5-9B has the best ask record**: 4 passes and 5 marginals of 11, two of them forced answers that passed, and the one research pass among the small models. Qwen3.5-4B matches its passes and has no marginals. Both are ahead of `qwen3-8b` (2 / 2 / 7), as on the key and unlike Enron.
- **The research phase held again**: 56 research units, all finished, no note-extraction timeout, no reaper interruption. On 2026-09-27 gpt-oss-20b lost three of seven to #720; today it lost none.
- **The forced answer is prose**: 35 asks were stopped (27 by time, 8 by Qwen3.6's context) and every one came back as an answer, six of which passed. Qwen3.8-27B was stopped on all 11 asks (median 13 minutes each) and still passed three of them.
- **Qwen3.6 at 16K refused one forced call** with HTTP 400, "request (21,228 tokens) exceeds the available context size (16,384)": #774, the third corpus to show it.

### What the numbers do not support

- A ranking of the eight: 18 units, one judge, and run-to-run movement of four passes on an unchanged model.
- A verdict on aggregates: they need a tool that counts (`documents_by_date` was the first) and the models to use it; this run shows neither happening reliably, not that it cannot.
- Anything about exact recall: no key.

### Cost

USD 7.79: 18 Claude baselines for USD 7.5659 (96 calls, 3432117 cache-read tokens, 950687 cache-write, 55450 out) and 143 verdicts for USD 0.2212. No documents were generated: the corpus is the one from 2026-09-26.

### Not verified

- Why the French forms fail: nobody has read the French baseline against the local answers, or checked what `fts_fr` returned for them.
- Whether Gemma 4 26B-A4B's drop from 6 to 2 passes is the judge or the model: the same corpus, the same judge model, a build that differs after a stop only; the answers themselves have not been diffed.
- Every model ran through the app with its defaults.

### Where this leaves the eight

Nothing promoted or retired. Three corpora now have every registered model on one build: the key ranks (Qwen3.5-4B, gpt-oss-20b and Gemma 26B at the top, Qwen3 far below), Enron is level, synthetic is flat and noisy. The Qwen3.5 swap for the small tiers is supported by two of three and contradicted by none; that PR can follow. Qwen3.8-27B stays as registered (#776). The instance holds the synthetic corpus and `gpt-oss-20b` is active again.
