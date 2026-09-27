# Benchmark run `bench-20260926-1816-synthetic` on ix, 2026-09-27

- Suite commit: `6da31e0` (the commit that ran the sweep; this report was rendered at `36cb0a5`). **Resumed at other commits: 36cb0a5, b89c6c1**
- Run started: 2026-09-26T22:16:15Z
- Corpora: synthetic
- Cloud spend: USD 23.14 of a 25.00 cap over 525 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **pass** at 2026-09-26T22:16:14Z, llama.cpp pin v0.4.1.

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | forced | forced pass | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | `gemma4-26b-a4b` | 18 | 18 | 14 | 4 | 4 | 1 | 0 | 0.49 | 0.12 | 185 | 14 | 6 | 5 | 3 | 3.07 | 1.71 | n/a |
| synthetic | `gpt-oss-20b` | 18 | 18 | 12 | 3 | 2 | 0 | 3 | 0.53 | 0.07 | 210 | 12 | 4 | 2 | 6 | 2.33 | 1.42 | n/a |
| synthetic | `qwen3-8b` | 18 | 18 | 18 | 0 | 0 | 0 | 0 | 0.34 | 0.10 | 143 | 18 | 2 | 3 | 13 | 1.33 | 1.11 | n/a |
| synthetic | `qwen36-35b-a3b` | 18 | 18 | 8 | 10 | 10 | 0 | 0 | 0.49 | 0.07 | 204 | 8 | 2 | 0 | 6 | 1.62 | 1.12 | n/a |

**16 answers were forced by the app** (10 by context budget, 6 by time budget): it stopped the search and answered from what it had, saying so. They are judged, counted under `forced` and `forced pass`, and included in `degraded`; `judged` and the columns after it are the answers the model chose to give.

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 36 | 184 | 256 | 2047745 | 6491943 | 106424 | 15.8312 |
| judge | 60 | 60 | 171581 | 0 | 0 | 42326 | 0.0851 |
| synthetic_doc | 281 | 281 | 23425 | 0 | 0 | 476805 | 7.2223 |

## Reading

**The synthetic corpus, regenerated after #732 so that it contains the vendors, clients and campaign the questions name: 280 documents (260 text, 20 by OCR), 2,173 chunks, 16 questions in 18 units, all four curated models, build `6da31e0`, the first build with the time budget (#730), the client keepalive (#731) and `documents_by_date` in chat (#723).** 72 model units ran: 52 finished, 17 degraded, 3 errored. Cloud spend USD 23.14 of 25, of which USD 7.62 bought nothing (below). No answer key applies to this question set.

The run took four launches over 13 hours, and each stop was a defect this report can name:

- 18:16 start, preflight a clean pass (the first: no swap, GPU idle). Generation stopped at document 245 when Sonnet returned a bilingual handbook's text as an object (#737, fixed by #738); the 244 documents were kept.
- 21:42 resume: the last 36 documents, then the sweep skipped re-ingest because the watched path and the count of 280 matched, while the index still held the corpus generated the day before: the app had scanned the folder at 18:15 and never observed the directory that replaced it a minute later (#739). 18 baselines, USD 7.62, were measured against the old documents (#740, fixed by #741). Stopped by hand at phase 4.
- 23:10 resume with the fix: it refused the index, naming all 280 files as newer than the scan, wiped, re-ingested (280 documents verified, every stage complete), and generated the 18 baselines again, passing the 30-minute token mark without a 401 (#729 in effect). Then Gemma failed to load: the server process left resident from the earlier stop had been killed by hand without deactivating through the API, and the launcher and the API disagreed about the model (#689's family). Stopped at 00:04.
- 00:55 resume after the app was restarted by hand: all 72 model units, no further stop, finished 07:17.

Four preflights: pass at 18:16 and 21:42; warn at 23:10 (3.1 GB of swap); fail at 00:55 because Gemma was already resident from the restart when the preflight ran, which is the resume's doing, not the machine's. The machine was otherwise quiet throughout, unlike the day before.

### The grid

Verdicts are the judge's. "forced" marks an answer the app stopped: at the 300-second time budget (`time`) or at the context budget (`context`); those are judged where the model produced text, and counted under `forced` in the table above, not under `judged`.

| question | shape | gemma4-26b-a4b | gpt-oss-20b | qwen3-8b | qwen36-35b-a3b |
|---|---|---|---|---|---|
| research-1 quarterly trends across departments | research | fail | fail | fail | fail |
| research-2 themes in onboarding documents | research | pass | pass | pass | pass |
| research-3 vendor relationships over the year | research | marginal | fail | fail | fail |
| research-4 policy changes and their rationale | research | fail | error | fail | fail |
| research-5 board agenda patterns over time | research | marginal | fail | fail | fail |
| research-6 major board decisions in Q3 (en) | research | marginal | error | marginal | fail |
| research-6 the same, in French | research | marginal | error | fail | fail |
| ask-1 price in the Q3 Globex Supplies contract | lookup | pass (forced, time) | pass | fail | forced, context |
| ask-2 who signed the 2025-06 handbook policy update | lookup | pass | forced, time | fail | forced, context |
| ask-3 documents about the Polestar Industries account | retrieval | pass | pass | marginal | forced, context |
| ask-4 invoices from Initech Software in 2025 | list | forced, time | marginal | marginal | forced, context |
| ask-5 total of invoices in March 2025 | aggregate | forced, time | fail | fail | forced, context |
| ask-6 policies updated in Q2 2025 | aggregate | pass | empty answer | fail | forced, context |
| ask-7 subject of the March 2025 board minutes (French) | lookup | fail | fail | fail | forced, context |
| ask-8 signatories of board meetings in 2025 | aggregate | forced, time | forced, time | fail | forced, context |
| ask-9 marketing copy on the Skylight campaign | retrieval | marginal | marginal | pass | pass |
| ask-10 the bilingual 2025 employee handbook (en) | retrieval | pass | fail | fail | forced, context |
| ask-10 the same, in French | retrieval | pass | pass | fail | forced, context |

### What the numbers support

- **The questions have targets now.** The four asks that were accidental negatives on the previous corpus (#732) are lookups here: of the 260 text documents, 25 name Globex, 14 Initech, 22 Polestar and 6 the Skylight campaign, and the baselines cite 11 to 24 documents for them instead of "none". Gemma passes the Globex price (inside a forced answer) and the Polestar account; gpt-oss-20b passes both; qwen3-8b, which was credited on both yesterday for agreeing there was nothing, fails the price and is marginal on the account.
- **The time budget works as a bound, and the forced answer under it does not work yet (#742).** Six units hit the 300-second budget, four of Gemma's and two of gpt-oss-20b's; every one ended within eight minutes, against 26 to 40 minutes for the same questions the day before, and the harness recorded each as degraded with the reason and sent the text to the judge. But five of the six forced answers are the app's fallback sentence: `api.log` shows the two-minute grace cut while the model was still thinking, before it wrote a word. The sixth, Gemma on the Globex price, finished inside the grace and passed. The budget delivers the bound it promised; the answer it promised is the next fix.
- **Gemma reached for `documents_by_date` and it paid once.** It called the tool on five asks (the handbook signatory, the March total, the Q2 policies, the French minutes, the signatories) and passed "which policies were updated in Q2 2025", the first aggregate any model has passed on any corpus, where the day before all twelve aggregate cells failed. The other two aggregates still fail for everyone: the March invoice total and the year's signatories need a sum and a roll-up the tool does not do. qwen3-8b called the tool once, on a question it did not fit; gpt-oss-20b and Qwen3.6 never called it.
- **Qwen3.6 at 16,384 tokens forced ten of eleven asks at its context budget, and none came back as an answer.** Eight of the ten are a tool call written as text after the app stopped offering tools (`<tool_call><function=read_passages>`), now caught by the roleplay signature (#735) and recorded degraded rather than judged as real; two are the fallback sentence. Its passes are the Skylight campaign and the onboarding themes. On a 32 GB Mac this model has 16K of context and a 280-document corpus fills it in two or three tool rounds; the reading is the same as on CUAD and Enron.
- **qwen3-8b finished all 18 units with no forced answer and no error, and passed two.** It is the model that never needs the bound and the one that finds least: 13 fails, the retrieval asks included. gpt-oss-20b is the reverse: it lost three of seven research units to #720's note-extraction silence (the harness's five-minute watchdog on a call the app's own reaper had let run), took 21 to 30 minutes for the four it finished, and has four passes, three of them asks.
- **Research is Gemma's, and only research-2 is anyone's.** Onboarding themes pass for all four; quarterly trends fail for all four; Gemma is marginal on four of the other five, qwen3-8b on one (the Q3 decisions in English), and everything else fails. Gemma's 6 pass, 5 marginal, 3 fail on the units it chose to answer is the best row on this corpus by a distance.
- **The bilingual handbook separates the models the same way as before.** Gemma finds it in either language; gpt-oss-20b in French only; qwen3-8b in neither; Qwen3.6 ran out of context both times.

### What the numbers do not support

- A ranking on pass counts: 6, 4, 2, 2 are made of different things. Gemma's include a forced answer and the only aggregate pass; gpt-oss-20b's row is missing three research units; qwen3-8b's two are honest and few; Qwen3.6's row is its context.
- Comparison with yesterday's synthetic report (`bench-20260925-2104-synthetic`): different corpus, different build, and four of eleven asks changed meaning. Only the shape carries over: aggregates fail, research-2 passes, Qwen3.6 is bounded by 16K.
- Anything about the six forced answers' content: five have none. `forced pass` is 1 of 6 for the time budget and 0 of 10 for the context budget, and both numbers are #742 before they are the models.
- The judge's rubric on French answers, as before.

### Cost

USD 23.14 of the 25 cap: 280 documents for USD 7.22 (the run's first pass bought 244, the resume 36 and one retried slot); 36 baselines for USD 15.83, of which the first 18 (USD 7.62) were generated against the wrong index and are not in this report's figures; 60 verdicts for USD 0.09. Without #740 the run would have needed a second run id to finish under the cap.

### Not verified

- What the forced answers would say with the grace measured from the first content token and thinking off (#742): this run's data is that five of six were cut before a word.
- Whether the March total and the signatories list are reachable with `documents_by_date` plus a summing step, or need a tool of their own: no model tried a second aggregate strategy.
- The OCR subset and the French rubric, as in the previous report.
- Why gpt-oss-20b returned an empty answer on the Q2 policies question after four tool calls with no stop reason; `llm.log` was not read for it.

### Where this leaves the four models

The harness now measures what it meant to: every unit ends, a forced answer is counted apart from a chosen one, and an index the sweep did not build is not trusted. What it measured on this build: Gemma is the model to give a mailbox-sized corpus, with the caveat that four of its eleven asks ran to the budget; the budget's forced answer is broken for thinking models (#742) and that is the next app fix; `documents_by_date` earned its place in chat with one pass and four attempts; Qwen3.6 on a 32 GB Mac remains a 16K model; gpt-oss-20b's research needs #720 before its row means anything. No registry change.
