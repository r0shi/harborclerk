# Benchmark run `bench-20260924-2233-keyed-gemma` on ix, 2026-09-24

- Suite commit: `7cbc4dc`
- Run started: 2026-09-25T02:33:45Z
- Corpora: cuad
- Cloud spend: USD 0.02 of a 25.00 cap over 20 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **pass** at 2026-09-25T02:33:30Z, llama.cpp pin v0.4.1.

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cuad | `gemma4-26b-a4b` | 20 | 20 | 20 | 0 | 0 | 0.39 | 0.19 | 54 | 20 | 13 | 2 | 5 | 3.95 | 3.60 | 0.84 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| judge | 20 | 20 | 31008 | 0 | 0 | 9582 | 0.0177 |

## Reading

**One model, one change: Gemma 4 26B-A4B on the 20 keyed CUAD questions, on the build that carries #715, against the same baselines as `bench-20260924-0724-keyed` (#714).** The 20 Claude baselines are the files that run produced (copied; USD 0 here); the preflight passed with the machine at rest before the start; all 20 units ran and finished. The point of the run is the answer-key column beside #714's.

### Before and after

| Gemma 4 26B-A4B | #714 (build `67dd3e5`) | this run (build `7cbc4dc`) |
|---|---|---|
| answer key, all 20 | 0.24 | **0.84** |
| fact (8) | 0.12 | **1.00** |
| date (6) | 0.17 | **1.00** |
| list (6) | 0.46 | 0.47 |
| judge pass / marginal / fail | 3 / 2 / 15 | **13** / 2 / 5 |
| answers-question (0–5) | 1.25 | 3.95 |
| "I cannot find a document titled …" answers | 12 | **0** |
| median latency | 20 s | 54 s |

### What changed and what did not

- **Gemma did not change; the tool did.** It still opens 13 of the 14 single-contract questions with `verify_identifier` on the display name from the question (the 14th it searched directly). Before #715 that call answered `not_found` with an instruction not to search, and Gemma stopped. Now the display name resolves to its filing name by words, the result says it was a loose match and to confirm the title, and Gemma goes on to search inside that document and cite the clause with its page range: 14 of 14 facts and dates at 1.00, every one judged pass but one marginal. The 54 s median is the cost of doing the work the 20 s decline skipped.
- **The lists did not move**, and were never this defect: Gemma's list answers were real attempts in #714 (0.46) and are real attempts here (0.47), with the same spread (0.00 to 0.92) and the same judge verdicts (four fail, one marginal, one pass in each run, on mostly the same questions). The list column is where Gemma still trails gpt-oss-20b (0.62) and the Claude baseline (0.64).
- **The other three models were not re-run.** They opened every single-contract question with `search_documents` in #714 and never met the branch this fix changes. A `matched_by: "words"` result can now reach any model that calls `verify_identifier` on a loose name; whether that ever produces a wrong `unique` on a real corpus is the risk the instruction and the token-equality rule are there for, and this run shows 13 loose matches, all to the right document.

### What the numbers support

With this run beside #714, Gemma's row on the key reads 0.84 against gpt-oss-20b's 0.89, Qwen3.6's 0.65 (at the 32 GB tier's 16K context) and qwen3-8b's 0.60. The difference between Gemma and gpt-oss-20b is now the list column alone. Combined with #710, where Gemma was the one model to do well at Research, the picture of the model has changed from "does not look for the contract" to "the best local model on this machine for research and equal to the others on lookup, weaker at lists".

### What they do not support

- A comparison of judge columns with runs before #695, or of this run's judge column with #714's beyond the pass count: same judge, same rubric, same baselines, so those are comparable; nothing older is.
- A claim about `verify_identifier` on other corpora: CUAD's filing names carry the display name's words in camel case; a corpus whose titles are OCR text or email subjects will behave differently, and the edges the reviews named (French stopwords, a possessive inside a title, a run split inside a camel run) were not exercised here.

### Not verified

- Whether Gemma would have answered from `search_documents` alone if `verify_identifier` had been absent; the run measures the product as shipped.
- Why `cuad-key-16` fell from 0.43 to 0.00 on the list side: six searches and an answer the key did not credit; one question, one run, inside the list column's noise.

### Where this leaves the four models

All four curated models have a judged row and a keyed row on CUAD on a healthy index, with the two defects the keyed run exposed either fixed (#711, this run) or filed with a measured cause (#712, Qwen3.6's 16K context). The remaining item from #702's plan is the other three corpora under the current judge.
