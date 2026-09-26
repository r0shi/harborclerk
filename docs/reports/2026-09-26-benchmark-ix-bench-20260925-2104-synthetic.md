# Benchmark run `bench-20260925-2104-synthetic` on ix, 2026-09-26

- Suite commit: `587d117` (the commit that ran the sweep; this report was rendered at `08e71c3`). **Resumed at other commits: 08e71c3**
- Run started: 2026-09-26T01:04:18Z
- Corpora: synthetic
- Cloud spend: USD 15.19 of a 25.00 cap over 436 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **warn** at 2026-09-26T01:04:17Z, llama.cpp pin v0.4.1.
  - warn: swap in use: 6124 MB
  - warn: GPU at idle: 69% busy before anything is run: a screensaver, a dynamic wallpaper or a screen-sharing session is rendering. Set the screensaver to Never and let the display sleep.

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | `gemma4-26b-a4b` | 18 | 18 | 15 | 0 | 3 | 0.38 | 0.13 | 268 | 15 | 4 | 3 | 8 | 2.13 | 1.80 | n/a |
| synthetic | `gpt-oss-20b` | 18 | 18 | 14 | 0 | 4 | 0.50 | 0.10 | 391 | 14 | 6 | 1 | 7 | 2.71 | 1.79 | n/a |
| synthetic | `qwen3-8b` | 18 | 18 | 17 | 0 | 1 | 0.30 | 0.19 | 114 | 17 | 5 | 4 | 8 | 2.41 | 1.53 | n/a |
| synthetic | `qwen36-35b-a3b` | 18 | 18 | 18 | 0 | 0 | 0.39 | 0.08 | 116 | 18 | 2 | 2 | 14 | 1.33 | 0.94 | n/a |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 18 | 92 | 128 | 960110 | 3391167 | 46110 | 7.4700 |
| judge | 64 | 64 | 153545 | 0 | 0 | 46644 | 0.0867 |
| synthetic_doc | 280 | 280 | 20410 | 0 | 0 | 504874 | 7.6343 |

## Reading

**The synthetic corpus: 280 generated documents for a fictional firm (260 text, 20 rendered to low-quality PDF and read by OCR), 2,241 chunks, 16 questions in 18 units (two are asked in English and French), all four curated models, build `7cbc4dc`, judge gpt-5.6-luna against a Claude baseline with the same tools.** 91 units were planned and ran: the corpus, 18 baselines, and 72 model units of which 64 finished and 8 errored. No answer key applies to this question set.

This run took three launches and surfaced four defects before its first verdict. Generation ran from 21:04 to 23:27 (280 documents, USD 7.63); the index then came out at 560 documents, because the generator had written each document's ground-truth facts as a JSON sidecar into the watched folder, and the app indexed the answer key beside the corpus. The run was stopped by hand before phase 1 (#726, fixed by #727). Resumed from main at 00:48, the harness moved the 280 sidecars out, refused the 560-document index, wiped and re-ingested 280 documents with every stage complete, and started the baselines. At 01:18, thirty minutes after login, the 14th baseline died on a 401: the MCP session held a token captured once (#681, fixed by #729). Resumed at 01:20 on a fresh login, the run finished at 09:55. Three preflights, all warn: swap at 6.1, 4.0 and 2.7 GB, and the GPU 69 to 72 percent busy before anything ran, which the preflight attributes to a screensaver, a dynamic wallpaper or a screen-sharing session. Every latency below was measured against that load; `llm.log` has Gemma generating at 11 to 14 tokens a second throughout.

### The grid

| question | shape | gemma4-26b-a4b | gpt-oss-20b | qwen3-8b | qwen36-35b-a3b |
|---|---|---|---|---|---|
| research-1 quarterly trends across departments | research | pass | fail | fail | fail |
| research-2 themes in onboarding documents | research | pass | marginal | marginal | marginal |
| research-3 vendor relationships over the year | research | marginal | error | fail | fail |
| research-4 policy changes and their rationale | research | fail | fail | fail | fail |
| research-5 board agenda patterns over time | research | fail | error | marginal | fail |
| research-6 major board decisions in Q3 (en) | research | fail | fail | error | fail |
| research-6 the same, in French | research | fail | error | fail | fail |
| ask-1 price in the Q3 Globex Supplies contract | no such document | error | pass | pass | fail |
| ask-2 who signed the 2025-06 handbook policy update | lookup | marginal | pass | pass | fail |
| ask-3 documents about the Polestar Industries account | no such document | error | pass | marginal | fail |
| ask-4 invoices from Initech Software in 2025 | no such document | pass | pass | fail | fail |
| ask-5 total of invoices in March 2025 | aggregate | fail | fail | fail | fail |
| ask-6 policies updated in Q2 2025 | aggregate | fail | fail | fail | fail |
| ask-7 subject of the March 2025 board minutes (French) | lookup | marginal | fail | pass | fail |
| ask-8 signatories of board meetings in 2025 | aggregate | fail | fail | fail | fail |
| ask-9 marketing copy on the Skylight campaign | no such document | error | error | pass | marginal |
| ask-10 the bilingual 2025 employee handbook (en) | retrieval | pass | pass | pass | pass |
| ask-10 the same, in French | retrieval | fail | pass | marginal | pass |

### What the numbers support

- **Four of the eleven asks look for things the corpus does not contain (#732).** The generator declares the firm's vendors, clients and campaigns and tells the model to use them, but never puts the names in the prompt; Sonnet invented its own (the invoices are from Staples, Steelcase and LexisNexis; no document mentions Globex, Initech, Polestar or a Skylight campaign). The questions were written against the declared names. Those four rows are negatives: the baseline answers "there are no documents in the corpus matching Initech Software" and a model passes by agreeing. gpt-oss-20b agreed three times and timed out on the fourth; qwen3-8b agreed twice; Gemma agreed once and spent 30, 26 and 16 minutes searching for the other three until the client gave up; Qwen3.6 failed all four.
- **No model answers an aggregate question, again.** Totals of invoices in a month, policies updated in a quarter, signatories across a year: 12 of 12 fail, as on Enron. The baseline read 29 to 52 documents for each. The chat surface on this build has no tool that filters by date or counts, and `documents_by_date` (#723) is not in it.
- **The two lookups and the bilingual retrieval separate the models.** Every model finds the bilingual handbook asked for in English; asked in French, Gemma does not. The French board-minutes question is passed by qwen3-8b alone. Who signed the June handbook update: gpt-oss-20b and qwen3-8b pass, Gemma is marginal, Qwen3.6 fails.
- **Qwen3.6 at 16,384 tokens loses seven of eleven asks to its context (#712).** `api.log` shows the loop forcing a text response at 99 to 118 percent of the budget after three or four tool rounds, seven times, all in its window. Four answers are the canned "wasn't able to formulate a complete response" (recorded `done`, #684 item 3); three end in a tool call written as text, `<tool_call><function=search_documents>`, which the harness's roleplay signature does not recognise (#733) and the judge failed. Its 2 pass, 2 marginal, 14 fail is the context, not the model, the same reading as the keyed CUAD run.
- **Research is Gemma's again, and nobody's.** Gemma passes two of seven and is marginal on a third; the others pass none. Policy changes over the year and the Q3 board decisions, in either language, fail for all four. gpt-oss-20b lost three of its seven research units to #720 (below) and its four that finished took 11 to 20 minutes each.
- **The eight errors are two known defects, not the questions.** Gemma's three and gpt-oss-20b's ask-9 are the harness's 600-second read timeout firing during one llama-server generation of about 8,000 tokens: the model was writing a tool call, or thinking, the app forwards neither, and its keepalive fires only when the model is silent, so the client heard nothing for ten minutes (#731, fixed in #730 with the time budget of #719). gpt-oss-20b's three research errors and qwen3-8b's one are note extraction: the 180-second call timeout fires, the call is retried, and the stream is silent past the harness's five-minute watchdog (#720); all four are research-3, -5 and -6, the questions whose notes run longest.

### What the numbers do not support

- A ranking of the models on this corpus: four asks are accidental negatives, three are aggregates no model can do with these tools, seven of Qwen3.6's are its context, and eight units did not finish. The pass counts (4, 6, 5, 2) are made of different things per model.
- Comparison with any earlier synthetic figure: there is none published, and this is the first run in which the documents carry no sidecar metadata (#727), so `metadata_filter`, `verify_identifier` and `documents_by_date` had no sidecar facts to draw on. That is intended (metadata mirroring the key would measure reading the key), but it also means no number here is the old condition's.
- Latency as the models' own: the GPU was busy with rendering for the whole run. Medians of 268, 391, 114 and 116 seconds say which model is slower than which, not how fast any is.
- Anything from the citation overlap column: the baseline cites 0 to 107 documents per question, and four of its answers are "none".

### Cost

USD 15.19: 280 documents for USD 7.63 (505 K output tokens, USD 0.027 a document, against the runbook's USD 3 to 5 estimate for the corpus); 18 Claude baselines for USD 7.47 (92 calls, 3.4 M cache-read tokens, USD 0.42 a question); 64 verdicts for USD 0.09. The sweep's pre-run estimate was USD 13.85.

### Not verified

- Whether the four empty-target asks become lookups on a corpus that contains their entities: that needs the generator fixed (#732) and a new corpus, about USD 8.
- Whether the aggregates change with `documents_by_date` in chat: the next run on a build with #723.
- The OCR subset: 20 PDFs were rendered at low quality and read by OCR; no question targets them specifically, and their extraction was not inspected.
- The French answers were judged by the same judge with the same rubric as the English ones; whether it grades French prose the same way was not checked.
- What the three Gemma timeouts were generating for 8,000 tokens (tool-call arguments or thinking); `llm.log` shows the length and rate, not the content.

### Where this leaves the four models

Nothing changes in the registry on this run. Its value is what it found: the harness indexed its own answer key (#726), the MCP token expiry (#681) recurred and is fixed, the client is starved during a long tool-call generation (#731), the synthetic corpus does not contain what its questions ask about (#732), and the roleplay signature misses Qwen3's format (#733), plus #720 confirmed on a second and third model. Of the models: qwen3-8b is the one that finished everything on time and passed the French lookup; gpt-oss-20b is the most exact on the lookups it finishes and the slowest at everything; Gemma is the only one that does Research at all and the one that loops longest on a question with no answer; Qwen3.6 on a 32 GB Mac is still a 16K model. The next synthetic run needs #732's corpus and a build with #723 and #730 before its numbers say more than this one's.
