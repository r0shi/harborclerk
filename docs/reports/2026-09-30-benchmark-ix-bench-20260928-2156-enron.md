# Benchmark run `bench-20260928-2156-enron` on ix, 2026-09-30

- Suite commit: `4fbf724`
- Run started: 2026-09-29T01:56:54Z
- Corpora: enron
- Cloud spend: USD 7.87 of a 25.00 cap over 212 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **warn** at 2026-09-29T01:56:53Z, llama.cpp pin v0.4.1.
  - warn: swap in use: 3522 MB

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | forced | forced pass | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enron | `gemma4-12b` | 16 | 16 | 16 | 0 | 0 | 0 | 0 | 0.18 | 0.11 | 160 | 16 | 5 | 3 | 8 | 2.44 | 1.56 | n/a |
| enron | `gemma4-26b-a4b` | 16 | 16 | 13 | 3 | 3 | 0 | 0 | 0.21 | 0.10 | 246 | 13 | 7 | 1 | 5 | 3.00 | 1.85 | n/a |
| enron | `gpt-oss-20b` | 16 | 16 | 14 | 2 | 2 | 1 | 0 | 0.31 | 0.09 | 235 | 14 | 4 | 4 | 6 | 2.29 | 1.71 | n/a |
| enron | `qwen3-8b` | 16 | 16 | 16 | 0 | 0 | 0 | 0 | 0.14 | 0.11 | 142 | 16 | 6 | 2 | 8 | 2.25 | 1.56 | n/a |
| enron | `qwen35-4b` | 16 | 16 | 13 | 3 | 3 | 0 | 0 | 0.23 | 0.18 | 227 | 13 | 4 | 5 | 4 | 2.92 | 2.08 | n/a |
| enron | `qwen35-9b` | 16 | 16 | 13 | 3 | 3 | 1 | 0 | 0.37 | 0.19 | 299 | 13 | 5 | 3 | 5 | 2.54 | 1.92 | n/a |
| enron | `qwen36-35b-a3b` | 16 | 16 | 10 | 6 | 6 | 2 | 0 | 0.30 | 0.20 | 254 | 10 | 3 | 2 | 5 | 2.80 | 2.00 | n/a |
| enron | `qwen38-27b` | 16 | 16 | 7 | 9 | 9 | 2 | 0 | 0.20 | 0.20 | 1153 | 7 | 3 | 1 | 3 | 3.00 | 2.43 | n/a |

**26 answers were forced by the app** (6 by context budget, 20 by time budget): it stopped the search and answered from what it had, saying so. They are judged (except the app's own sentences for a model that then produced nothing, a model server that failed, or a thought cut short, which have no answer to judge), counted under `forced` and `forced pass`, and included in `degraded`; `judged` and the columns after it are the answers the model chose to give.

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 16 | 86 | 118 | 998629 | 3433842 | 44929 | 7.6962 |
| judge | 126 | 126 | 336198 | 0 | 0 | 87819 | 0.1726 |

## Reading

**The Enron corpus (10,576 emails, 39,829 chunks), 16 questions (10 ask, 6 research), all eight registered models, one build: app `9a425ca`, harness `4fbf724`.** 128 model units and 16 baselines, no error, no restart, one launch, 21 h 46 min of which 8 h 37 min were the corpus's summaries before the first baseline (Apple Intelligence, about twenty a minute), USD 7.87. Preflight warned on swap (3.5 GB) and passed everything else. There is no answer key for this corpus: every score is the judge's verdict against a Claude baseline with the same tools. The four incumbents have an Enron row from 2026-09-25 (build `7cbc4dc`, before #730, #742 and #755) to compare with.

### The grid

Judge verdicts as pass / marginal / fail, by question type; "forced" is the count of asks the app stopped at its time or context budget and answered from what it had.

| model | ask (10) | research (6) | forced | forced pass | median ask s | median research s | 09-25 (all 16) |
|---|---|---|---|---|---|---|---|
| `gemma4-26b-a4b` | 6 / 0 / 4 | 1 / 1 / 4 | 3 | 0 | 86 | 322 | 7 / 2 / 6, 1 error |
| `qwen35-9b` | 6 / 1 / 3 | 0 / 2 / 4 | 3 | 1 | 229 | 420 | new |
| `qwen3-8b` | 5 / 1 / 4 | 1 / 1 / 4 | 0 | | 98 | 319 | 3 / 5 / 8 |
| `gemma4-12b` | 5 / 1 / 4 | 0 / 2 / 4 | 0 | | 83 | 536 | new |
| `gpt-oss-20b` | 5 / 1 / 4 | 0 / 3 / 3 | 2 | 1 | 96 | 564 | 5 / 1 / 7, 2 errors |
| `qwen35-4b` | 4 / 3 / 3 | 0 / 2 / 4 | 3 | 0 | 169 | 282 | new |
| `qwen36-35b-a3b` at 16,384 | 4 / 2 / 2, 2 unjudged | 1 / 1 / 4 | 6 | 2 | 72 | 291 | 4 / 3 / 9 |
| `qwen38-27b` | 3 / 3 / 4 | 2 / 1 / 3 | 9 | 2 | 665 | 1,287 | new |

### What the numbers support

- **The research phase no longer loses units (#720, #755).** On 2026-09-25 three of the sixteen research units on the two big models ended in error, two of them gpt-oss-20b's note extraction running past the reaper. This run: 48 research units, 48 finished; `api.log` has no note-extraction timeout, no reaper interruption, and the longest notes call was 565 s, inside the 600 s the fix allows. The keepalive did what it was for.
- **The question decides more than the model.** Four questions fail for all eight models (`ask-2`, `ask-10`, `research-3`, `research-6`) and three pass for seven or eight (`ask-3`, `ask-5`, `ask-6`). The spread between models on the remaining nine is where the ranking lives, and it is narrow: 4 to 6 ask passes for six of the eight.
- **Qwen3.5 does not separate from Qwen3 on this corpus the way it did on the key.** On the keyed CUAD questions the 4B scored 0.85 against `qwen3-8b`'s 0.43; here the 4B has 4 ask passes to the 8B's 5, the 9B 6, and the three are within one pass on research. Enron asks are open questions over 10,000 emails, not lookups with one right answer, and the judge is comparing prose to prose. The 9B has the best citation and entity overlap with the baseline (0.37, 0.19), which is the one column that favours the Qwen3.5 and Qwen3.6 family here (0.18 to 0.20 entity overlap against 0.09 to 0.11 for the rest).
- **Qwen3.8-27B is the best research model in the set and the slowest thing in it.** Two research passes (the most), 3 of 10 asks passed (two of them forced answers), 3.00 on answers-question; but 9 of 10 asks were forced by the time budget, a median ask took 11 minutes and a median research question 21, at 5.9 tokens a second decode and 57 prefill. Two of its forced answers passed the judge. The research pipeline plans the searches and this model executes them exactly; the ask loop leaves it to plan and the budget runs out.
- **Qwen3.6 at 16K hit the wall twice.** Two of its ten asks ended in the app's server-failure sentence: llama-server refused the forced final call with HTTP 400, "request (19,240 / 17,887 tokens) exceeds the available context size (16,384)". That is #774, seen on all three corpora now. The other four context-budget stops came back as answers and two passed, which #742's "search is over" message accounts for.
- **The forced answer is prose everywhere.** 26 asks were stopped, 20 by the time budget and 6 by Qwen3.6's context budget; apart from the two refusals above, every one came back as an answer the judge could read, and six passed. On 2026-09-25 the same stops ended in the harness's read timeout or the fallback sentence.
- **Gemma 4 12B does not displace gpt-oss-20b here either**: 5 / 3 / 8 against 4 / 4 / 6, no research pass for either, 2.44 against 2.29 on answers-question. The retirement question in #551 stays open.

### What the numbers do not support

- A ranking: 16 questions, one judge, and six models within two passes of each other. The 2026-09-25 rows for the same four models moved by one to three passes with no model change, which is the size of the run-to-run noise.
- Anything about lists or exact facts: no answer key exists for Enron.
- The Qwen3.5 swap on its own: supported on the key, level here. The synthetic run (same day) is the third corpus.

### Cost

USD 7.87: 16 Claude baselines for USD 7.6962 (86 calls, 3433842 cache-read tokens, 998629 cache-write, 44929 out) and 126 verdicts for USD 0.1726. The estimate before the run was USD 8.22.

### Not verified

- Why the four all-fail questions fail: whether the corpus lacks the answer, the baseline has it and the judge is strict, or the questions are unanswerable as written. Nobody has read those four baselines side by side with the answers.
- Whether Qwen3.8-27B's research advantage survives a faster Mac; here every research question ran near the time limit.
- The 8 h 37 min summary wait is the corpus size at Apple Intelligence's rate and is not part of any model's number; a run that skipped summaries would measure a different corpus (#717).
- Every model ran through the app with its defaults; nothing was tuned per model.

### Where this leaves the eight

Nothing promoted or retired on this corpus. The two structural fixes this run was the first to exercise at scale, #755 (research heartbeat) and #742 (forced answer), both held. #774 is the open defect the run exposed, three times across the corpora. The instance holds Enron and `gpt-oss-20b` is active again.
