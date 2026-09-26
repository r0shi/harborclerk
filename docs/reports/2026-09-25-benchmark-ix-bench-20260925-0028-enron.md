# Benchmark run `bench-20260925-0028-enron` on ix, 2026-09-25

- Suite commit: `7cbc4dc` (the commit that ran the sweep; this report was rendered at `587d117`). **Resumed at other commits: 587d117**
- Run started: 2026-09-25T04:28:37Z
- Corpora: enron
- Cloud spend: USD 7.32 of a 25.00 cap over 143 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **warn** at 2026-09-25T04:28:20Z, llama.cpp pin v0.4.1.
  - warn: swap in use: 3720 MB

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enron | `gemma4-26b-a4b` | 16 | 16 | 15 | 0 | 1 | 0.18 | 0.10 | 291 | 15 | 7 | 2 | 6 | 2.93 | 1.80 | n/a |
| enron | `gpt-oss-20b` | 16 | 16 | 13 | 1 | 2 | 0.29 | 0.09 | 418 | 13 | 5 | 1 | 7 | 2.54 | 1.85 | n/a |
| enron | `qwen3-8b` | 16 | 16 | 16 | 0 | 0 | 0.11 | 0.11 | 147 | 16 | 3 | 5 | 8 | 2.12 | 1.31 | n/a |
| enron | `qwen36-35b-a3b` | 16 | 16 | 16 | 0 | 0 | 0.26 | 0.10 | 238 | 16 | 4 | 3 | 9 | 1.88 | 1.31 | n/a |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 16 | 83 | 115 | 972490 | 2610515 | 41850 | 7.2462 |
| judge | 60 | 60 | 147454 | 0 | 0 | 39029 | 0.0763 |

## Reading

**The Enron corpus: 10,576 emails (39,829 chunks), 16 questions (6 research, 10 ask), all four curated models, build `7cbc4dc`, the same judge and baseline model as the CUAD runs.** 64 units were planned and 64 ran: 60 finished, one degraded, three errors. There is no answer key for this corpus, so every score is the judge's verdict against a Claude baseline that had the same tools.

The run did not go straight through. The sweep started at 00:28, waited for the pipeline to drain, and gave up after four hours: the pipeline still had 5,745 summaries queued, which Apple Intelligence produces at about 20 a minute, and the harness counted them as work that gates search (#717). The watcher resumed the run at 09:08 with the backlog at zero and the index complete: every document extracted, chunked, tagged and embedded, and 10,503 of 10,576 summarised. Apple Intelligence refused the other 73. Since #718 the sweep waits for summaries by default with a progress bound, records the backlog beside each unit, and this run predates that column, which is why the resumed rows carry no value for it. The second preflight, at the resume, was the same warn as the first (swap 2.1 GB against 3.7 GB at 00:28, everything else passing). Four units were rerun from `587d117` at 20:48 after #721 (below); that preflight added a second warn, the GPU at 77% before anything ran, which the preflight attributes to a screensaver, a dynamic wallpaper or a screen-sharing session; it affects the latencies of those four units only.

### The grid

| question | shape | gemma4-26b-a4b | gpt-oss-20b | qwen3-8b | qwen36-35b-a3b |
|---|---|---|---|---|---|
| research-1 topics of October 2001 | research | marginal | fail | fail | marginal |
| research-2 Skilling and the California crisis | research | pass | pass | marginal | pass |
| research-3 communication patterns before bankruptcy | research | fail | fail | fail | fail |
| research-4 accounting practices over time | research | marginal | fail | marginal | marginal |
| research-5 major business deals | research | fail | error | fail | marginal |
| research-6 external counterparties | research | fail | fail | fail | fail |
| ask-1 who emailed Skilling most in 2001 | aggregate | fail | error | fail | fail |
| ask-2 earliest email about California | date boundary | fail | fail | fail | fail |
| ask-3 emails about Raptor | retrieval | pass | pass | marginal | pass |
| ask-4 emails about LJM | retrieval | pass | pass | marginal | fail |
| ask-5 emails containing "off-balance-sheet" | retrieval | pass | pass | pass | fail |
| ask-6 companies mentioned most often | aggregate | pass | pass | marginal | pass |
| ask-7 emails mentioning Arthur Andersen | retrieval | pass | marginal | pass | fail |
| ask-8 emails forwarded by Lay in 2001 | aggregate | fail | fail | fail | fail |
| ask-9 emails about FERC | retrieval | pass | fail | pass | pass |
| ask-10 Skilling's last email before resigning | date boundary | error | degraded | fail | fail |

### What the numbers support

- **Every model can do "find emails about X".** On the five retrieval-shaped asks (3, 4, 5, 7, 9) Gemma passes all five, `qwen3-8b` passes three and is marginal on two, `gpt-oss-20b` passes three with one marginal and one fail, Qwen3.6 passes two and fails three. This is the shape of question a mailbox user asks most, and the two smaller-footprint models do it as well as the two big ones.
- **No model answers a question that needs the whole corpus, unless a tool has already counted it.** The two date-boundary asks (2 and 10) and the two per-sender aggregates (1 and 8) fail for all four; the one aggregate that passes for three of four (6, "which companies are mentioned most") is the one where `entity_overview` hands the model a ranked count. Every model read Enron at 18,127 mentions and The New York Times at 1,129 from that tool and stopped there, and the judge gave answers-question 5 to two of them and 4 to a third for it. The four failures are not the models' reasoning: chat had no tool that orders documents by date or filters by sender. `documents_by_date` exists for MCP callers and was missing from the chat surface (#722, fixed by #723 after this run); ask-1 and ask-8 need a sender filter that no surface has.
- **The Claude baseline is not a ceiling here, it is a different answer.** It cites between 2 and 506 documents per question (506 on ask-1, 159 on research-6, 137 on research-5), so citation overlap is 0.11 to 0.29 for every model and says little about who was right. On ask-1 the baseline read 506 documents to count senders; no local model tried, and none would have finished if it had.
- **Research is where the models separate least.** research-2 is the only research question three models pass; research-3 and research-6 fail for all four; the rest are marginal for Gemma and Qwen3.6 and fail for `gpt-oss-20b`. Gemma's 7 pass, 2 marginal is the best row, as it was on CUAD research (#710); `gpt-oss-20b`, the best model on the keyed CUAD facts (#714), is the weakest here at Research and the slowest, median 418 s with research-6 at 1,807 s.
- **ask-6 verdicts come from a rerun.** In the original pass the harness judged the ask-6 baseline unusable because it contained "the corpus appears to be", which matched an empty-corpus signature meant for "the corpus appears to be empty"; the four units finished but were never judged (#721). After the fix the four units were rerun from `587d117` against the same build and index: three pass, one marginal. The judge count in the spend table (60) and the header's 143 calls include those four.

### What the numbers do not support

- A ranking of the four models on this corpus: 16 questions, one judge, no key, and six of the sixteen are questions no model can answer with the tools chat had. The pass counts (7, 5, 3, 4) are dominated by the retrieval asks.
- Anything about `gpt-oss-20b`'s Research at this build beyond "slow and often silent": its research-5 error is the harness aborting after 328 s with no event from the server, on the build that carries #703's heartbeats. `llm.log` places the silence in note extraction (#720, below); the fan-out itself never went quiet.
- Anything about latency for the four ask-6 rerun units: the GPU was busy before they ran.

### Two timeouts, two issues

- **Gemma on ask-10 ran for 71 minutes and 19 model calls before the harness's ten-minute stream read timeout ended it (#719).** The route's keepalives held the stream open while the model paged through results; the error at 4,263 s is the first ten minutes with nothing at all. `gpt-oss-20b` on ask-1 went the same way at 20 minutes and 12 calls, recorded at 1,191 s; on ask-10 it stopped itself after 12 minutes with the canned "wasn't able to formulate a complete response" and status `completed`, which the harness recorded as degraded (#712's shape). A chat answer is bounded by context and by tool rounds, not by time; the judge saw none of the three.
- **`gpt-oss-20b` writes 5,000-token research notes at about 16 tokens a second, so a note-extraction call runs five minutes against a 120 s timeout that retries it (#720).** During the retry nothing touches the heartbeat and no event is sent, so both the reaper and the harness watchdog read a working call as a stall. That is the research-5 error, and it is the slow part of the model's 418 s median on the research units that did finish.

### Cost

USD 7.32: 16 Claude baselines for USD 7.25 (83 calls, 2.6 M cache-read tokens, 972 K cache-write, 42 K out; USD 0.45 a question, twice the CUAD rate because the baseline reads hundreds of emails for the aggregates), and 60 verdicts for USD 0.08. The ask-6 rerun cost four verdicts.

### Not verified

- Whether the four aggregate and boundary failures become passes with `documents_by_date` in chat (#723): the build under test did not have it. That is the first thing the next Enron run measures.
- Why Apple Intelligence refused 73 summaries: the documents finalised without them and are searchable; the refusals were not read.
- Why the pipeline queued 5,745 summaries after the index otherwise completed, when the CUAD runs never showed a backlog: this corpus is ten times the size, and the summariser is the slowest stage, but the numbers were not compared.
- `qwen3-4b`, the Qwen3.5 pair and Gemma 4 12B: not downloaded on this machine, not run.

### Where this leaves the four models

On CUAD the story was facts versus research (#714). On a mailbox it is retrieval versus everything else: all four find emails, none can count or order them, and the fix for half of that is a tool the chat surface did not expose, not a model. Gemma is again the best Research model and the only one to pass every retrieval ask, and also the model that spent 71 minutes on one question, so #719 is for it first; `gpt-oss-20b` is the slowest and the one whose Research needs #720; Qwen3.6 at 16K on a 32 GB Mac fails three retrieval asks the 8B model does not. No registry change follows from this run.
