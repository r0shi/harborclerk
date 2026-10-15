# Benchmark run `bench-20261015-0106-synthetic-astra` on ix, 2026-10-15

- Suite commit: `f265869` (the commit that ran the sweep; this report was rendered at `2a30221`)
- Run started: 2026-10-15T05:06:49Z
- Corpora: synthetic
- Cloud spend: USD 8.16 of a 35.00 cap over 99 calls; judge gpt-5.6-luna; baseline gpt-6-astra; prices as of 2026-10-14.
- Machine preflight: **pass** at 2026-10-15T05:06:48Z, llama.cpp pin v0.4.1.

## Cloud subject

The phase-1 baseline, scored against the corpus's answer key as phase 4 is (no judge, no reference), over every keyed question it answered. `calls` are model calls; `USD` is the run's baseline_question spend from spend.json, which is this row's when the run has one subject on one corpus (n/a otherwise).

| corpus | model | questions | calls | stopped at call limit | answer key (0-1) | fact | date | list | amount | count | USD |
|---|---|---|---|---|---|---|---|---|---|---|---|
| synthetic | `gpt-6-astra` | 23 | 99 | 0 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 8.1647 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 23 | 99 | 297 | 454139 | 1664168 | 16417 | 8.1647 |

Cache writes by lifetime, as priced: baseline_question 454,139 at five minutes, 0 at an hour.

Thinking among the output tokens: baseline_question 1,110 of 16,417.

## Reading

**The fourth cloud-subject run, closing the plan's first frontier measurement: gpt-6-astra answering the 20 keyed synthetic questions (23 units, three asked in English and French) through Harbor Clerk's MCP tools, scored against the key written from the corpus's own fact sidecars (#812), no judge.** App build `a40dbe9` (v0.9.3), the 2026-09-26 generation (`44741ee75b545bbd`, 280 documents) as Fable's run had ingested it (verified, not re-ingested), 99 model calls, 9 minutes, USD 8.16 against a pre-run estimate of 25.78, none stopped at the call limit. The reading of what the synthetic runs show is in Fable's report (`2026-10-15-benchmark-ix-bench-20261015-0032-synthetic-fable.md`); this one records what differs.

### The grid

| subject | facts (5) | dates (5) | counts (4) | amounts (4) | lists (5) | all 23 | calls | wall time | USD |
|---|---|---|---|---|---|---|---|---|---|
| `gpt-6-astra` (this run) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 99 | 9 min | 8.16 |
| `claude-fable-5-1` (same night) | 1.00 | 1.00 | 1.00 | 1.00 | 0.99 | 1.00 | 107 | 15 min | 20.31 |

### What the numbers support

- **Every unit right, including the five aggregates and the three French forms.** 15 Initech invoices, USD 364,107.38 for Cyberdyne over 25 invoices, USD 25,474.18 for March, 11 Q3 board meetings, 7 Harbour Lights briefs, every list exactly the key's set with nothing extra, every French variant answered in French. On 2026-09-30 every local model failed every aggregate and every French form; both frontier models now pass all of them through the same tools, so the tool surface is not what stops the local models.
- **astra's answers are terse where Fable's narrate**, which is why astra scores 1.00 on the list where Fable scored 0.96: astra names only the matches, Fable also names what it rejected in prose (#816). Read by a human the two sets of answers are equally right.
- **Price and speed.** USD 8.16 against 20.31, 9 minutes against 15: 80% of astra's prompt tokens came from OpenAI's cache at a tenth of the input price, its reasoning was 1,110 tokens over 99 calls, and its answers are short (16,417 output tokens against Fable's 58,409). The estimate of 25.78 assumed Sonnet 4.6's volumes scaled up and 1,500 thinking tokens a call; both were far too high for this model on this corpus. `spend.yaml`'s provisional figures should be corrected from the four ledgers of 2026-10-15.
- **Responses API, second run.** 99 calls, no refusal, no incomplete, every tool round trip accepted; cached and written tokens metered as on CUAD.

### What the numbers do not support

- Anything about research-type questions or judgment: the keyed set is lookups and aggregates by design.
- A reasoning figure for astra until the Responses usage field is confirmed against a known call; the cost is unaffected.

### Not verified

No human read the 23 answers end to end; every unit was checked against the sidecar-derived key. The run used the v0.9.3 app build.
