# Benchmark run `bench-20261015-2259-cuad-fable-rerun` on ix, 2026-10-15

- Suite commit: `db16201`
- Run started: 2026-10-16T02:59:44Z
- Corpora: cuad
- Cloud spend: USD 16.32 of a 35.00 cap over 81 calls; judge gpt-5.6-luna; baseline claude-fable-5-1; prices as of 2026-10-14.
- Machine preflight: **pass** at 2026-10-16T02:59:34Z, llama.cpp pin v0.4.1.

## Cloud subject

The phase-1 baseline, scored against the corpus's answer key as phase 4 is (no judge, no reference), over every keyed question it answered. `calls` are model calls; `USD` is the run's baseline_question spend from spend.json, which is this row's when the run has one subject on one corpus (n/a otherwise).

| corpus | model | questions | calls | stopped at call limit | answer key (0-1) | fact | date | list | USD |
|---|---|---|---|---|---|---|---|---|---|
| cuad | `claude-fable-5-1` | 20 | 81 | 0 | 0.95 | 1.00 | 1.00 | 0.84 | 16.3177 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 20 | 81 | 202 | 1012824 | 4045288 | 52882 | 16.3177 |

Cache writes by lifetime, as priced: baseline_question 1,012,824 at five minutes, 0 at an hour.

Thinking among the output tokens: baseline_question 12,320 of 52,882.

## Reading

_Written by whoever ran it: what the numbers support, what they do not, and what was not verified._

**The keyed CUAD run of Claude Fable 5.1 repeated on a build carrying the search fixes, to measure what they recover.** App build `db16201` from main, same 20 questions, same model, 81 model calls, 13 minutes, USD 16.32 against a pre-run estimate of 21.71, none stopped at the call limit. The first run (`2026-10-15-benchmark-ix-bench-20261014-2328-cuad-fable.md`) used the v0.9.3 build `a40dbe9`. Between the two builds, three changes touch what a model retrieves: `text_contains` ignores differences in whitespace (#819, #824), brief `kb_find_all` rows carry a bounded passage and its chunk id (#820, #822), and the full-text columns are built over whitespace-folded text (#825, #827). The corpus was ingested on the earlier build and carried forward by migrations 0008 and 0009, which rebuild the derived columns those fixes read.

### Before and after

| question | before F1 (P / R) | after F1 (P / R) |
|---|---|---|
| key-15 customer non-solicitation | 0.62 (0.80 / 0.50) | 0.80 (0.86 / 0.75) |
| key-16 non-disparagement | 0.67 (0.67 / 0.67) | 0.43 (0.60 / 0.33) |
| key-17 irrevocable or perpetual licence | 1.00 | 1.00 |
| key-18 right of first refusal, offer or negotiation | 0.67 (1.00 / 0.50) | 0.83 (0.92 / 0.75) |
| key-19 insurance | 1.00 | 1.00 |
| key-20 Delaware governing law | 1.00 | 1.00 |
| lists (6) | 0.82 | 0.84 |
| all 20 | 0.95 | 0.95 |

Facts and dates stayed at 1.00.

### What the numbers support

- **key-18 gained four contracts, and one of them is the whitespace fix directly.** Both runs sent the same literal sweep, `text_contains: "first opportunity"`. On `a40dbe9` it returned no rows; on `db16201` it returned WaterNow's Distributor Agreement. Whitespace folding is the only change to that filter between the builds, so the phrase's words must be separated by a line break or non-breaking space in the extracted text (#819). NakedBrand came from a sweep the first run never sent (`"right to negotiate"`), and Revolution Medicines was in the first run's results without being listed, so those gains are partly the model choosing differently on a second try.
- **key-15 gained Rand Worldwide and Usio by reading the passages the brief rows now carry.** After two `kb_find_all` calls and no other search, Fable called `kb_read_passages` on chunk ids that only the brief rows could have supplied, which is what #822's tool description tells it to do. The first run reached the same titles but never read them. These are two of the three clauses astra found and Fable missed on 2026-10-15, so two thirds of the gap between the two models on key-15 is closed. Gentech is still missed.
- **The key-16 drop is the scorer, not retrieval.** All three contracts lost (Berkshire Hills, Cybergy, Digital Cinema) are named in the new answer, under a heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)". #814's exclusion rule drops any section whose heading carries "borderline" or "not a", so they no longer count. The first run put the same three under "One-way non-disparagement covenants embedded in other sections", "Disparagement as a termination trigger only" and "Related but narrower", all of which the rule keeps. Scored with the exclusion rule off, both answers get F1 0.63 (0.60 / 0.67), identical. The rule cannot tell a model's "weaker but still a match" section from its "not a match" section; this is filed on #816.
- **Retrieval surfaced 70 of the 71 list positives, against 71 of 71 before.** The one not surfaced is Innerscope's Distributor Agreement on key-16, which neither run's answer named. Where answers improved, the improvement was in what the model read and listed, not in what the tools could reach.
- **Cost came in under the estimate.** USD 16.32 for 81 calls, against 21.46 for 86 calls on the first run. Fable thought 12,320 tokens over 81 calls, about 150 a call, below the 250 that `spend.yaml` now assumes.

### What the numbers do not support

- A claim that the list column moved: 0.82 to 0.84 is within what one rerun of one model can move by choosing different sweeps. key-15 and key-18 rose by 0.16 to 0.18 each; key-16 fell by 0.24 through the scorer. Only the WaterNow gain is tied to a fix by the same query returning different rows.
- Anything about astra, the synthetic corpus or local models on this build; only Fable on keyed CUAD was rerun.
- That the semantic ceiling moved. The clauses that carry no keyword (the diagnostic of 2026-10-15) remain missed; the gains are clauses that do carry their words.

### Not verified

No human read the 20 answers end to end. The tool transcripts store a 600-character summary of each result, so the chunk-id provenance on key-15 rests on there being no other source of chunk ids before those calls, not on reading the full rows. The WaterNow line break is inferred from the fix, not read from the stored chunk. One run per build; variance between two runs on the same build was not measured.

Since 2026-10-18 the universe is 80 (#815): rescored over 80, this run's lists are 0.838 (0.843 before; key-18 0.828 → 0.800, and the first run's key-18 0.667 → 0.640), and the before-and-after list figures (0.82 and 0.84) read the same at two places; see `2026-10-18-rescore-cuad-key-universe-80.md`.
