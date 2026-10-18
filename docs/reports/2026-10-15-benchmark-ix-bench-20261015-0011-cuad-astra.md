# Benchmark run `bench-20261015-0011-cuad-astra` on ix, 2026-10-15

- Suite commit: `f265869` (the commit that ran the sweep; this report was rendered at `2a30221`)
- Run started: 2026-10-15T04:12:09Z
- Corpora: cuad
- Cloud spend: USD 20.66 of a 35.00 cap over 98 calls; judge gpt-5.6-luna; baseline gpt-6-astra; prices as of 2026-10-14.
- Machine preflight: **pass** at 2026-10-15T04:11:10Z, llama.cpp pin v0.4.1.

## Cloud subject

The phase-1 baseline, scored against the corpus's answer key as phase 4 is (no judge, no reference), over every keyed question it answered. `calls` are model calls; `USD` is the run's baseline_question spend from spend.json, which is this row's when the run has one subject on one corpus (n/a otherwise).

| corpus | model | questions | calls | stopped at call limit | answer key (0-1) | fact | date | list | USD |
|---|---|---|---|---|---|---|---|---|---|
| cuad | `gpt-6-astra` | 20 | 98 | 0 | 0.96 | 1.00 | 1.00 | 0.87 | 20.6594 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 20 | 98 | 294 | 1187574 | 4650106 | 23233 | 20.6594 |

Cache writes by lifetime, as priced: baseline_question 1,187,574 at five minutes, 0 at an hour.

Thinking among the output tokens: baseline_question 1,748 of 23,233.

## Reading

**The second cloud-subject run, the same night as Fable's: gpt-6-astra answering the 20 keyed CUAD questions through Harbor Clerk's MCP tools, scored against the answer key with no judge.** App build `a40dbe9` (v0.9.3), the CUAD corpus Fable's run had ingested (verified at 80 documents, not re-ingested), 20 questions, 98 model calls, 13 minutes, USD 20.66 against a pre-run estimate of 22.42, none stopped at the call limit. This is the first run through OpenAI's Responses API, which GPT-6 requires for tool calling (#811). The reading of what the two frontier runs show is written once, in Fable's report (`2026-10-15-benchmark-ix-bench-20261014-2328-cuad-fable.md`); this one records what differs.

### The grid

| subject | facts (8) | dates (6) | lists (6) | all 20 | calls | wall time | USD |
|---|---|---|---|---|---|---|---|
| `gpt-6-astra` (this run) | 1.00 | 1.00 | 0.87 | 0.96 | 98 | 13 min | 20.66 |
| `claude-fable-5-1` (same night) | 1.00 | 1.00 | 0.82 | 0.95 | 86 | 25 min | 21.46 |
| `claude-sonnet-4-6` (2026-09-28, rescored) | 1.00 | 1.00 | 0.76 | 0.93 | 78 | 17 min | 6.61 |

Per list question (F1, precision, recall): key-15 customer non-solicitation 0.82 (0.78 / 0.88); key-16 non-disparagement 0.67 (0.67 / 0.67); key-17 irrevocable or perpetual licence 1.00; key-18 right of first refusal, offer or negotiation 0.74 (0.91 / 0.62); key-19 insurance 0.98 (0.95 / 1.00); key-20 Delaware governing law 1.00.

### What the numbers support

- **The Responses API path works as built from the documentation.** 98 calls, every tool round trip and reasoning item echoed back accepted, `stopped_by` end_turn on all 20, cached input recognised and priced (4.65 million cached tokens of 5.84 million prompt tokens), 1.25x cache writes metered. The one figure to doubt is thinking: the usage blocks reported 1,748 reasoning tokens over 98 calls, which is either a model answering these questions with almost no reasoning or a field the meter reads wrongly; the cost is unaffected either way (reasoning bills as output, and output was 23,233 tokens in total).
- **astra and Fable agree on every fact and date and are within 0.05 on lists.** Where they differ on lists, astra found three customer non-solicit clauses that have no "solicit" in them (Gentech, Rand Worldwide, Usio), all three labelled by CUAD's annotators on wording such as "shall not contact any of Distributor's Customers"; Fable found none of the three. On the other four list questions they miss the same contracts: the clauses annotated without the keyword, which neither model's literal sweep nor semantic probes reached. See the Fable report for the list of those and of the extras both models share.
- **astra is faster and its extras are labelled.** 13 minutes against 25, with more calls (98 against 86) but shorter ones; precision 0.67 to 1.00 per list against Fable's 0.67 to 1.00. Like Fable it names eDiets on the non-disparagement question while saying it is not one ("One additional keyword match, not counted above"); the rule of #814 drops a section headed that way, so it is not counted. Its other extras carry their own caveat in the answer ("Limited to use of the marks", "an express procurement requirement; the clause does not separately specify an ongoing maintenance period"), so a reader can tell a hedged inclusion from a confident one; the key cannot.
- **The list rule of #814 is what makes these figures.** By the name-only rule the report first gave astra 0.37 on lists; like Fable it names contracts by parties and document id, and some of its answers happened to include a few filename stems. Earlier cloud list scores were floors.
- **Cost.** USD 20.66 against the estimate of 22.42; the OpenAI figure was flagged as a floor because the estimate assumed Sonnet's cache split would carry over to OpenAI's automatic caching, and it did: 80% of prompt tokens were served from the cache. The provisional 1,500 thinking tokens a call did not materialise and should be corrected from this ledger.

### What the numbers do not support

- A ranking of astra over Fable: six list questions, a 0.05 gap, and the gap on key-15 is three contracts.
- Anything beyond CUAD's single-hop facts, dates and enumerations; the synthetic key run is next.
- The reasoning-token figure as a measure of how much astra reasoned, until the field is confirmed against a known call.

### Not verified

No human read the 20 answers end to end. The run used the v0.9.3 app build. The universe is 79 of 80 contracts (#815).

Since 2026-10-18 the universe is 80: rescored over 80, this run's lists are 0.871 (key-18 0.741 → 0.759, key-19 0.977 → 0.978) and its list and overall means read the same at two places; see `2026-10-18-rescore-cuad-key-universe-80.md`.
