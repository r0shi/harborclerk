# Benchmark run `bench-20261014-2328-cuad-fable` on ix, 2026-10-15

- Suite commit: `f265869` (the commit that ran the sweep; this report was rendered at `2a30221`)
- Run started: 2026-10-15T03:43:35Z
- Corpora: cuad
- Cloud spend: USD 21.46 of a 35.00 cap over 86 calls; judge gpt-5.6-luna; baseline claude-fable-5-1; prices as of 2026-10-14.
- Machine preflight: **pass** at 2026-10-15T03:43:20Z, llama.cpp pin v0.4.1.

## Cloud subject

The phase-1 baseline, scored against the corpus's answer key as phase 4 is (no judge, no reference), over every keyed question it answered. `calls` are model calls; `USD` is the run's baseline_question spend from spend.json, which is this row's when the run has one subject on one corpus (n/a otherwise).

| corpus | model | questions | calls | stopped at call limit | answer key (0-1) | fact | date | list | USD |
|---|---|---|---|---|---|---|---|---|---|
| cuad | `claude-fable-5-1` | 20 | 86 | 0 | 0.95 | 1.00 | 1.00 | 0.82 | 21.4641 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 20 | 86 | 212 | 1349506 | 5704528 | 63341 | 21.4641 |

Cache writes by lifetime, as priced: baseline_question 1,349,506 at five minutes, 0 at an hour.

Thinking among the output tokens: baseline_question 20,768 of 63,341.

## Reading

**The first cloud-subject run: Claude Fable 5.1 answering the 20 keyed CUAD questions through Harbor Clerk's MCP tools, scored against the answer key with no judge.** App build `a40dbe9` (v0.9.3), suite commit above, 80 contracts ingested fresh, 20 questions, 86 model calls, 25 minutes, USD 21.46 against a pre-run estimate of 24.11, none stopped at the call limit. This is goal 5 of the current plan: does the tool surface let a frontier model answer comprehensively, and if not, what limits it. A second run the same night asked gpt-6-astra the same questions (`2026-10-15-benchmark-ix-bench-20261015-0011-cuad-astra.md`); the two are read together.

### The grid

| subject | facts (8) | dates (6) | lists (6) | all 20 | calls | wall time | USD |
|---|---|---|---|---|---|---|---|
| `claude-fable-5-1` (this run) | 1.00 | 1.00 | 0.82 | 0.95 | 86 | 25 min | 21.46 |
| `gpt-6-astra` (same night) | 1.00 | 1.00 | 0.87 | 0.96 | 98 | 13 min | 20.66 |
| `claude-sonnet-4-6` (2026-09-28, rescored) | 1.00 | 1.00 | 0.76 | 0.93 | 78 | 17 min | 6.61 |
| best local, `gpt-oss-20b` (2026-09-28, 5-minute budget) | 1.00 | 1.00 | 0.58 | 0.87 | | | |

Per list question (F1, precision, recall): key-15 customer non-solicitation 0.62 (0.80 / 0.50); key-16 non-disparagement 0.67 (0.67 / 0.67); key-17 irrevocable or perpetual licence 1.00; key-18 right of first refusal, offer or negotiation 0.67 (1.00 / 0.50); key-19 insurance 1.00; key-20 Delaware governing law 1.00.

### What the numbers support

- **Facts and dates are solved at this tier.** Every governing-law fact and every date, first try, as Sonnet 4.6 and the best local models also manage. The tool surface is not what limits single-hop questions.
- **The list column was mis-scored, for every cloud model, until this run.** The report generator first gave Fable 0.16 on lists. Fable names a contract by its parties and then the document id in backticks, never by the filename stem the key matches on, so the rule saw no items at all in four of six answers that listed 9 to 21 contracts. #814 lets a document id written in an answer name that document through the answer's own citations, and stops reading at a heading that marks exclusion ("False positive (excluded)", "not counted above", "near-miss"), which both frontier models use; the figures above are from that rule. Sonnet 4.6's September figure moves from 0.64 to 0.76 under it. Local models write filename stems and no ids, so no local score changes. Every earlier report's list column for a cloud model was a floor.
- **Enumeration reaches 0.82 to 0.87 F1 for the two strongest models available, and they land within 0.05 of each other.** Model choice is not the lever on this corpus. Both ran the same strategy: a literal `kb_find_all` sweep for the clause's keyword across all 80 contracts ("I ran a literal-text filter for 'disparag' across the whole corpus (10 documents, all 21 matching chunks read)"), a few semantic probes for synonyms, then a read of every candidate. Where the keyword is in the text, both are nearly perfect (irrevocable licences, insurance covenants and Delaware law at 0.98 to 1.00).
- **What they miss is the clause the annotator saw without the keyword.** The CUAD positives both models missed have no instance of the word in the extracted text (23,000 to 300,000 characters each, so not an extraction loss): "shall not do anything which is harmful to its honor, goodwill or reputation" is labelled non-disparagement; "the exclusive right to negotiate for continued endorsement" is a right of first negotiation; "shall not contact any of Distributor's Customers" is a customer non-solicit. Six of the sixteen right-of-first-refusal positives, three of nine non-disparagement positives and one of eight non-solicit positives are of this kind, and both models missed all of them. That is the ceiling the tool surface sets today: a literal find-all finds the words, and the semantic search did not find these clauses by meaning. astra recovered three non-solicit clauses of this kind that Fable did not (Gentech, Rand Worldwide, Usio), which is the whole gap between the two on key-15.
- **The extras split between gaps in the key and over-inclusion.** All three cloud subjects list Bizzingo's Endorsement Agreement for non-disparagement (its §19 is titled "Confidentiality and Non-Disparagement Agreement") and Stamps.com for customer non-solicit ("agrees not to use any Customer Data to solicit any MBE-Generated Customers"); the CUAD annotations do not, and the text says the models are right. All three also list Creditcards.com, and Fable and astra Rand Worldwide, for non-disparagement on a trademark-use clause ("not to use the Licensed Materials in any manner that is disparaging"), which is a reader's call. Fable's one list with precision below 1.0 after the exclusion rule is key-16 (0.67). Precision on lists is therefore partly the key's and partly the model's, and the key cannot say which without a reader.
- **One contract is outside the key's universe.** `HarpoonTherapeuticsInc_…_Development Agreement_Option Agreement` is ingested but its filename is truncated in `master_clauses.csv`, so the key describes 79 of the 80 contracts and both models' citations of it are ignored (#815). The scores above are over 79.
- **Since 2026-10-18 the universe is 80** (#815): rescored over 80, this run's lists are 0.820 (key-18 0.667 → 0.640) and its list and overall means read the same at two places, as do astra's; Sonnet 4.6's 2026-09-28 list figure in the grid becomes 0.77; see `2026-10-18-rescore-cuad-key-universe-80.md`.
- **Cost and the cap.** Fable thought 20,768 tokens over 86 calls, about 240 a call against the provisional 1,500 in `spend.yaml`; the estimate still landed within USD 3 because the search read more context than the Sonnet measurement it was scaled from (1.35 million cache-write tokens against 0.89 million). The 35.00 cap was not approached; at the old 25.00 the per-call reservation would have stopped this run around question 17. The provisional thinking figure should be corrected from this ledger (`thinking_tokens / calls`).

### What the numbers do not support

- Any comparison with the list column of a report before 2026-10-15 for a cloud model; those were scored by name only.
- Anything about research-type questions, Enron or the synthetic corpus; the keyed set is single-hop facts, dates and enumerations over 80 contracts. The synthetic key (#812) is the next run.
- A verdict on precision: the extras need a reader to split annotation gaps from over-inclusion, and the eDiets case shows the rule counts a named non-match.
- That the semantic misses are a search defect rather than a question of what "non-disparagement" means: the key is CUAD's annotators' judgment, and a reader could argue several of the missed clauses either way.

### Not verified

No human read the 20 answers end to end; the facts and dates were checked by the key and the list extras by reading the clause text behind each. The run used the v0.9.3 app build, not current main (the MCP changes since, #801 and #807, affect redaction for keys, not what a model retrieves). Fable's per-call thinking is what the usage block reported; nothing independent confirms it.
