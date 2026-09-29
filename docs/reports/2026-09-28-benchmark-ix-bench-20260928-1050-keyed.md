# Benchmark run `bench-20260928-1050-keyed` on ix, 2026-09-28

- Suite commit: `9a425ca`
- Run started: 2026-09-28T14:50:20Z
- Corpora: cuad
- Cloud spend: USD 6.75 of a 25.00 cap over 236 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **pass** at 2026-09-28T14:50:20Z, llama.cpp pin v0.4.1.

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | forced | forced pass | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cuad | `gemma4-12b` | 20 | 20 | 18 | 2 | 2 | 0 | 0 | 0.38 | 0.18 | 98 | 18 | 14 | 0 | 4 | 4.22 | 3.28 | 0.89 |
| cuad | `gemma4-26b-a4b` | 20 | 20 | 15 | 5 | 5 | 0 | 0 | 0.43 | 0.17 | 48 | 15 | 14 | 0 | 1 | 4.80 | 3.60 | 0.97 |
| cuad | `gpt-oss-20b` | 20 | 20 | 15 | 5 | 5 | 0 | 0 | 0.58 | 0.05 | 25 | 15 | 14 | 0 | 1 | 4.80 | 3.40 | 0.97 |
| cuad | `qwen3-8b` | 20 | 20 | 19 | 1 | 1 | 0 | 0 | 0.37 | 0.18 | 89 | 19 | 7 | 0 | 12 | 2.21 | 1.89 | 0.45 |
| cuad | `qwen35-4b` | 20 | 20 | 15 | 5 | 5 | 0 | 0 | 0.56 | 0.46 | 54 | 15 | 14 | 1 | 0 | 4.87 | 4.40 | 0.96 |
| cuad | `qwen35-9b` | 20 | 20 | 14 | 6 | 6 | 0 | 0 | 0.55 | 0.44 | 66 | 14 | 13 | 1 | 0 | 4.79 | 4.14 | 0.97 |
| cuad | `qwen36-35b-a3b` | 20 | 20 | 12 | 8 | 7 | 1 | 0 | 0.55 | 0.44 | 37 | 12 | 12 | 0 | 0 | 5.00 | 4.17 | 1.00 |
| cuad | `qwen38-27b` | 20 | 20 | 12 | 8 | 8 | 2 | 0 | 0.59 | 0.51 | 201 | 12 | 12 | 0 | 0 | 5.00 | 4.33 | 1.00 |

**39 answers were forced by the app** (7 by context budget, 32 by time budget): it stopped the search and answered from what it had, saying so. They are judged (except the app's own sentences for a model that then produced nothing, a model server that failed, or a thought cut short, which have no answer to judge), counted under `forced` and `forced pass`, and included in `degraded`; `judged` and the columns after it are the answers the model chose to give.

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 20 | 78 | 118 | 885157 | 2946578 | 27779 | 6.6120 |
| judge | 158 | 158 | 217976 | 0 | 0 | 80243 | 0.1399 |

## Reading

**Stage 3 of model-refresh (#551): the keyed CUAD questions, all eight registered models on one build (`9a425ca`, the first with #742, #753, #755 and #765), the machine passing every preflight check for the first time.** 20 questions with a human answer key (8 governing-law facts, 6 dates, 6 lists of contracts), 160 model units, 20 Claude baselines, no error, no restart, one launch, 10 h 20 min, USD 6.75. The four models added for evaluation (`qwen35-4b`, `qwen35-9b`, `gemma4-12b`, `qwen38-27b`) ran for the first time; the four incumbents have a keyed row on `67dd3e5` (2026-09-24) to compare with.

### The answer key, over all 20 answers

The table's "answer key" column is over the answers the model chose to give; this one is over everything the user received, forced answers included, which is the figure that matters. Decode speed is token-weighted from `llm.log`; latency is the median wall time of a fact or date question.

| model | fact (8) | date (6) | list (6) | all 20 | 09-24 | decode tok/s | fact/date median s | forced |
|---|---|---|---|---|---|---|---|---|
| Claude baseline (`claude-sonnet-4-6`, scored offline) | 1.00 | 1.00 | 0.64 | 0.89 | 0.89 | | 18 | |
| `gpt-oss-20b` | 1.00 | 1.00 | 0.58 | **0.87** | 0.89 | 29.9 | 41 / 25 | 5 |
| `gemma4-26b-a4b` | 1.00 | 1.00 | 0.49 | 0.85 | 0.24 | 24.4 | 50 / 47 | 5 |
| `qwen35-4b` | 1.00 | 1.00 | 0.49 | 0.85 | new | 22.8 | 63 / 46 | 5 |
| `qwen38-27b` | 1.00 | 1.00 | 0.47 | 0.84 | new | 5.9 | 262 / 187 | 8 |
| `gemma4-12b` | 1.00 | 1.00 | 0.41 | 0.82 | new | 12.2 | 76 / 83 | 2 |
| `qwen35-9b` | 1.00 | 0.83 | 0.52 | 0.81 | new | 15.9 | 83 / 62 | 6 |
| `qwen36-35b-a3b` at 16,384 | 0.88 | 1.00 | 0.43 | 0.78 | 0.65 | 24.6 | 42 / 32 | 8 |
| `qwen3-8b` | 0.50 | 0.50 | 0.25 | 0.43 | 0.60 | 17.4 | 93 / 73 | 1 |

### What the numbers support

- **Qwen3.5-4B is the small-tier result.** 1.00 on every fact and date, 0.49 on lists, 14 pass and 1 marginal from the judge, no fail, at 2.7 GB and 23 tokens a second: level with Gemma 4 26B-A4B on this key at a sixth of the size. Qwen3.5-9B is a shade behind it (one date lost to a forced answer) and a third slower. Both are far above the Qwen3 tiers they were added to succeed: `qwen3-8b` scored 0.43 and the judge failed 12 of its 19 chosen answers. The mechanical swap the survey called for is supported by this run; a registry change is a separate PR.
- **Gemma 4 26B-A4B found the contracts this time.** On 2026-09-24 it answered 12 of 14 fact and date questions with "I cannot find a document titled …" (0.24, #711); today 14 of 14 at 1.00, with the same question text. Nothing in this run isolates the cause; the candidates on the build between the two runs are the word pass in the identifier tools (#715, then #723 and #752) and the time budget (#730). #711 should be re-read against this before anyone closes it.
- **The forced final answer is an answer now (#742).** 39 units were stopped by the app, 32 by the time budget and 7 by Qwen3.6's 16K context budget, and every one of the 32 came back as prose the judge could read: partial lists scoring 0.15 to 0.95 against the key; 13 of the 39 were judged marginal and 3 passed. On 2026-09-27 five of six forced answers were the fallback sentence. Qwen3.6's context-budget answers went from six fallbacks and 0.00 on lists to real answers and 0.43, which is the "the search has ended" message doing what #742 said it would.
- **The time budget decides the list column.** 35 of the 48 list units were stopped by the app: 29 by the time budget and Qwen3.6's 6 by its context. Qwen3-8B (1 of 6) and Gemma 4 12B (2 of 6) finished most lists inside five minutes; the other five models were stopped on 5 or 6 of 6. The list scores above are what a model assembles from the results it had when the clock stopped. The baseline, with no budget, scores 0.64 on the same lists. So the column ranks how much a model finds in five minutes, not how well it lists.
- **Qwen3.8-27B is exact and slow.** 12 of 12 chosen answers pass, key 1.00; 8 of 20 forced, two of them facts (key-1, key-6) that no other model needed forcing for. It decoded at 5.9 tokens a second and prefilled at 56, against 24 to 30 and 240 to 360 for the mixture-of-experts models, so a fact question took four minutes where Gemma took one, and a list question 16 to 19 minutes. The survey's bandwidth ceiling of 7 tokens a second was the right order. It ran at 72,704 tokens of context on this 32 GB Mac, as the registry said it would (`n_ctx_slot` in `llm.log`), and loaded without a Metal complaint. Not a model for the mini; a quality tier for a Mac with more memory bandwidth, or for speculative decoding (#551). No registry change.
- **`gpt-oss-20b` is still the model that gets the facts and the most of the lists**, 0.87 against 0.89 on the last build, and the fastest decoder here. Gemma 4 12B (0.82) does not displace it; the retirement question in #551 stays open.
- **Judge and key agree on every chosen answer**: no pass with a key below 0.5, no fail with a key of 1.0. The #713 rule fired four times: three `qwen3-8b` answers whose opening says the fact is not stated (all judged fail, key 0), and one Qwen3.6 date answer whose opening hedged before the model found the term (the scorer reads the first sentence only, and that sentence is not one of the phrasings; key 1.0, judge pass). Half of `qwen3-8b`'s fall from 0.60 is this rule; the other half is the model.

### Time the forced answer costs

A forced answer is bounded, but not by the numbers in the code. `_FINAL_ANSWER_THINKING_SECONDS` and the grace run from the model's first token; the prompt is re-read in full first, with no cache, and on Qwen3.8-27B that is 18,000 tokens at 56 a second: five and a half minutes before either clock starts. One traced conversation: budget spent at 19:04:52, answer cut at 19:12:14, seven minutes twenty after the bound. The slower models' forced questions therefore ran 8 to 19 minutes against a 5-minute budget. The outer bound should include the prompt re-read: #773.

### What the numbers do not support

- A verdict on lists: six questions, partial-credit F1, every model bounded by the time budget, and the baseline itself between 0.31 and 1.00 on them.
- Anything about research-type questions or the other three corpora: the keyed set is single-hop facts, dates and lists.
- A ranking among the 0.81 to 0.87 group: five models within six hundredths on 20 questions.
- `qwen3-4b`: not downloaded, not run.

### Not verified

- Why Gemma 4 26B-A4B finds the contracts now (above).
- Whether `qwen3-8b`'s drop beyond the scorer's share is the build or the day: the same questions, the same build for all eight, one run.
- Qwen3.6's two non-forced failures: `cuad-key-6` came back empty (a completed response with no text, the shape of #684 item 3), and `cuad-key-18`'s forced call was refused by llama-server with HTTP 400, "request (18224 tokens) exceeds the available context", which #742's server-failure wording made visible; the forced call on a 16K model must fit the context: #774.
- Every model was run through the app with the app's defaults; nothing was tuned per model.

### Cost

USD 6.75: 20 Claude baselines for USD 6.61 (78 calls, 2.9 M cache-read tokens, 885 K cache-write, 28 K out; USD 0.33 a question against the 2026-09-24 run's 0.23, with 11 more calls), and 158 verdicts for USD 0.14. The pre-run estimate was USD 10.28.

### Where this leaves the eight

Promote nothing on one corpus. Supported by this run and worth their own PRs: register Qwen3.5-4B as the small default in place of `qwen3-8b` (a non-keyed corpus first: Enron or synthetic, one run), and keep Qwen3.8-27B registered with its "not for this Mac" guidance until a faster Mac or a drafter measures it. The instance holds the CUAD corpus and `gpt-oss-20b` is active again, as it was before the run.
