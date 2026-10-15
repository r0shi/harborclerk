# Benchmark run `bench-20261015-0032-synthetic-fable` on ix, 2026-10-15

- Suite commit: `f265869` (the commit that ran the sweep; this report was rendered at `2a30221`)
- Run started: 2026-10-15T04:32:18Z
- Corpora: synthetic
- Cloud spend: USD 20.31 of a 35.00 cap over 107 calls; judge gpt-5.6-luna; baseline claude-fable-5-1; prices as of 2026-10-14.
- Machine preflight: **pass** at 2026-10-15T04:32:17Z, llama.cpp pin v0.4.1.

## Cloud subject

The phase-1 baseline, scored against the corpus's answer key as phase 4 is (no judge, no reference), over every keyed question it answered. `calls` are model calls; `USD` is the run's baseline_question spend from spend.json, which is this row's when the run has one subject on one corpus (n/a otherwise).

| corpus | model | questions | calls | stopped at call limit | answer key (0-1) | fact | date | list | amount | count | USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | `claude-fable-5-1` | 23 | 107 | 0 | 1.00 | 1.00 | 1.00 | 0.99 | 1.00 | 1.00 | 20.3085 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 23 | 107 | 260 | 1296968 | 4693218 | 58409 | 20.3085 |

Cache writes by lifetime, as priced: baseline_question 1,296,968 at five minutes, 0 at an hour.

Thinking among the output tokens: baseline_question 15,876 of 58,409.

## Reading

**The third cloud-subject run: Claude Fable 5.1 answering the 20 keyed synthetic questions (23 units, three asked in English and French) through Harbor Clerk's MCP tools, scored against the key written from the corpus's own fact sidecars (#812), no judge.** App build `a40dbe9` (v0.9.3), the 2026-09-26 generation of the corpus (`44741ee75b545bbd`, 280 documents) ingested fresh, 107 model calls, 15 minutes of answering after a 16-minute ingest, USD 20.31 against a pre-run estimate of 27.72, none stopped at the call limit. The questions are the ones every local model failed on 2026-09-30: counts and totals across the corpus, and the French forms.

### The grid

| subject | facts (5) | dates (5) | counts (4) | amounts (4) | lists (5) | all 23 | calls | USD |
|---|---|---|---|---|---|---|---|---|
| `claude-fable-5-1` (this run) | 1.00 | 1.00 | 1.00 | 1.00 | 0.99 | 1.00 | 107 | 20.31 |
| `gpt-6-astra` (same night) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 99 | 8.16 |

The one list unit below 1.00 is the rule's cost, not Fable's: key-8 (every invoice in February 2025) lists the 12 and adds "0048 is a July 2025 invoice — excluded" in running prose, which the rule counts as a 13th (0.96; #816). Read as a human would, every one of the 23 answers is right. The three French variants are right in French, with one to two more tool calls than their English twins.

### What the numbers support

- **The aggregates every local model failed are answered through the same tools.** How many invoices Initech issued in 2025 (15), the total Cyberdyne invoiced (USD 364,107.38 over 25 invoices), the total invoiced in March (two invoices, USD 25,474.18), board meetings in Q3 (11), briefs for Harbour Lights (7): all exact. Fable's method is visible in the answers: a `kb_find_all` sweep for the entity, a read of every hit, an explicit reconciliation ("Both are Initech Software invoices, not Cyberdyne — so the 25-invoice set is complete"), and on the two totals a second pass to confirm. On 2026-09-30 `ask-5` to `ask-8` (the same kinds of question, unkeyed) failed for all eight local models. The tool surface supports counting and summing across 280 documents; the local models do not do it.
- **The French failure is not retrieval.** #779 records that the French form of a synthetic question fails for every local model where the English passes for five. Here the French forms of a count, a date and a list all score 1.00, answered in French ("**Réponse : 15 factures** émises par Initech Software"). The index and the tools serve a French question; the defect is in the local models' handling of one. Noted on #779 with the transcripts to compare.
- **The key held.** Every count and amount matched to the cent in the model's own formatting (`$10,725.41`, `USD 364,107.38`), dates in both conventions, the invoice lists by number. Nothing in the 23 answers argued with the key, unlike CUAD, where the annotations and the models disagree on what counts as a clause. A generated corpus with machine-readable ground truth is the cleaner instrument; its cost is that the questions are lookups and aggregates, not judgment.
- **Cost.** Fable thought 15,876 tokens over 107 calls, about 150 a call, a tenth of the provisional 1,500 in `spend.yaml`; the estimate of 27.72 was high by a third for that reason. The corpus is cheaper per question than CUAD (shorter documents) despite more calls.

### What the numbers do not support

- Anything about research-type questions: the keyed set has none, by design.
- Precision on lists from the rule alone: see key-8 above and #816.
- A ranking against astra: astra scored 1.00 on all 23 units in nine minutes for USD 8.16, and Fable's one below-1.00 unit is the rule's cost, so on this corpus the two are level and the difference is speed and price.

### Not verified

No human read the 23 answers end to end; every scored unit was checked against the sidecar-derived key, and the two below 1.00 were read. The run used the v0.9.3 app build.
