# Judge bake-off, 2026-09-21

Asked by the owner before the re-baseline (#549): is the judge model (`claude-sonnet-4-6`, also the baseline
model) worth replacing with something newer, smaller or cheaper, and what about OpenAI's current prices? Answers
#661. Cloud spend: **USD 2.93** of a 3.00 cap, 500 verdicts.

## What was done

`scripts/test_corpora/judge_bakeoff.py` (new, in this PR) re-judged 50 answers that run `bench-20260921-0041`
left on disk: every non-empty answer from `gemma4-26b-a4b`, `gpt-oss-20b` and `qwen3-8b` to 16 CUAD questions,
plus two of `qwen36-35b-a3b`'s canned failures as a sanity check. Four judges were bought under the sweep's rubric
(`claude-sonnet-5`, `claude-haiku-4-5`, `gpt-5.6-terra`, `gpt-5.6-luna`) and the incumbent `claude-sonnet-4-6` was
bought once more; three of them were also bought under a **split rubric** that scores "does it answer the
question" apart from "does it cover the reference" and takes its verdict from the first. That run's retrieval was
broken (#685), which does not matter for comparing judges with each other: the answers span the whole range of
quality.

Two things about the material, both found in review of this report:

- **Sonnet 4.6's "first pass" is the sweep's own verdicts**, made the night before by the sweep's code path
  (1,500 output tokens, not 2,000) on the 49 of these 50 answers the sweep judged. The bake-off also judged one
  unit the sweep had marked degraded, which is why some denominators are 49. The tool now selects what the sweep
  selects.
- **The ground truth is human labels put through my scoring.** CUAD ships `master_clauses.csv`, expert
  annotations of 41 clause types per contract. Mine are: mapping seven of the ten "ask" questions onto a clause
  type (governing law, parties, term; California law, most-favoured-nation, anti-assignment, exclusivity);
  restricting the list questions to the 81 contracts ingested; matching contract names in an answer's text and
  citations, which is crude; scoring a fact as present-or-not and a list as F1; and calling a truth score of 0.8
  or more "right" and under 0.2 "wrong". None of that code, and none of the verdict files, is in the repository,
  so **no figure below can be re-derived from this PR**. They are on the mini, in the run's `bakeoff/` directory.

That gives 23 answers with a truth score: 7 right, 13 wrong, 3 between. They are three models on seven questions,
not 23 independent draws: every model got one question right, nearly every model got the list questions "wrong"
because F1 is strict on recall, so a judge that merely tracks question difficulty would correlate well. The
effective sample is closer to seven.

## Results

Price is per verdict as metered (about 2,500 tokens in, 400 out). "Rank correlation" is Spearman between the
truth score and the judge's `completeness` (sweep's rubric) or `answers_question` (split), over the 23 answers.

| judge | USD per verdict | same verdict on a second pass | same verdict as the sweep's Sonnet 4.6 | rank correlation with truth, sweep's rubric | split rubric |
|---|---|---|---|---|---|
| `claude-sonnet-4-6` (today) | 0.0139 | 49 of 49 | | 0.79 and 0.68 | not run |
| `claude-sonnet-5` (thinking off) | 0.0112 | not repeated | 45 of 49 | 0.78 | not run |
| `claude-haiku-4-5` | 0.0043 | 50 of 50 | 44 of 49 | 0.71 and 0.65 | 0.74 |
| `gpt-5.6-terra` | 0.0087 | not repeated | 46 of 49 | 0.84 | 0.79 |
| `gpt-5.6-luna` | **0.0010** | 48 of 50 | 44 and 46 of 49 | 0.81 and 0.83 | 0.79 |

All 500 bought verdicts parsed as JSON. Every judge's completeness correlates 0.92 to 0.96 with the sweep's
Sonnet 4.6.

**1. This experiment cannot tell the judges apart, and could not have.** Sonnet 4.6 gives 0.79 and 0.68 on two
passes whose verdicts are identical (the second includes one more answer): pass-to-pass noise as large as the
whole 0.65 to 0.84 spread between judges. Bootstrap intervals over the 23 answers all overlap, and they are too
narrow, because the answers are clustered by question. So "indistinguishable" is the finding, and it cuts both
ways: **a materially worse judge would not have been detected either.** What the data does show: the three
judges that were repeated are self-consistent, all five track the incumbent closely, and `gpt-5.6-luna` does that
at a fourteenth of the price. (Sonnet 5's list price is a third below Sonnet 4.6's; its tokenizer produces about
30% more tokens for the same text, and it metered 20% cheaper.)

**2. The sweep's rubric fails right answers; whether the split rubric is better depends on the judge.**
Verdicts on the 7 right and 13 wrong answers, as pass / marginal / fail:

| rubric | judge | 7 right answers | 13 wrong answers | right passed + wrong failed, of 20 |
|---|---|---|---|---|
| sweep's | `claude-sonnet-4-6` | 1 / 6 / 0 | 1 / 2 / 10 | 11 |
| sweep's | `claude-sonnet-5` | 1 / 3 / 3 | 1 / 2 / 10 | 11 |
| sweep's | `claude-haiku-4-5` | 1 / 4 / 2 | 0 / 2 / 11 | 12 |
| sweep's | `gpt-5.6-terra` | 2 / 5 / 0 | 1 / 2 / 10 | 12 |
| sweep's | `gpt-5.6-luna` | 4 / 3 / 0 | 1 / 2 / 10 | **14** |
| split | `claude-haiku-4-5` | 6 / 1 / 0 | 3 / 0 / 10 | **16** |
| split | `gpt-5.6-terra` | 5 / 2 / 0 | 2 / 4 / 7 | 12 |
| split | `gpt-5.6-luna` | 5 / 2 / 0 | 3 / 2 / 8 | 13 |

Under the sweep's rubric every Claude judge passes one right answer in seven: it grades coverage of the
baseline's territory, so right-and-short is "marginal" (right answers average 2.2 to 3.2 of 5). That is part of
why the last report showed one pass in sixteen for every model; the rest is that most answers that night *were*
wrong, because retrieval was broken. The split rubric fixes that for Haiku (1 to 6 right answers passed, 12 to 16
correct calls). **It does not help the OpenAI judges**: luna goes from 14 correct calls to 13 and terra stays at
12, because every judge also lets more wrong answers through under it (three passed, where at most one did
before). On these twenty answers a rubric that is simply more lenient would look the same. And the comparison is
confounded: my split prompt also dropped the words "Claude baseline" and "local LLM", told the judge the
reference might be incomplete, and defined score-to-verdict thresholds the sweep's prompt does not have. The
effect on the Claude judges cannot be attributed to splitting the score. Differences of two or three calls in
twenty are not significant.

**3. The reference is fallible, and a faithful judge inherits its errors.** Scored the same way, the Claude
baseline got the three single-contract facts right, and on the list questions reached F1 0.72 (California law),
0.50 (anti-assignment: 22 of the 23 it named are right, of 65 labelled), 0.13 (exclusivity) and **0.00 on
most-favoured-nation, where it stated that no contract in the corpus has such a clause and the annotators
labelled five**. By my scorer two local models out-scored the baseline on three of the seven questions; that
comparison leans hardest on the crude name matching and should be read as "the baseline is not reliably the
best answer", no more. The MFN statement needs no scorer. That run's broken retrieval understates what the
baseline can do, but the structure stands: a judge cannot know what the reference missed, and marks a model down
for finding it. Several "wrong answers passed" above are this in reverse: an answer naming three correct
anti-assignment contracts of 65 looks fine to any judge.

**4. Found in passing: `citation_overlap` has the wrong denominator.** The baseline's cited list
(`cited_doc_ids`, with `cited_doc_titles` beside it) is every document any of its tool calls returned, not what
its answer cites: 51 behind a 636-character answer that cites none. The metric is the share of that list the
model also cited, so an over-long list *lowers* it. (An earlier version of this report said "inflated". That was
backwards.)

## Reading

- **Judge model.** Nothing here says a newer or smaller judge is better or worse than Sonnet 4.6, and nothing
  could have at this size. So the choice can be made on price and independence: `gpt-5.6-luna` is fourteen times
  cheaper and from another vendor than the baseline it grades against. Under the **sweep's own rubric** it was
  also the judge that passed the most right answers (4 of 7, where the Claude judges passed 1), which hints at
  the Claude judges following "coverage of the Claude baseline" more literally, or at same-family preference;
  this data cannot separate those, or either from chance.
- **Rubric.** The sweep's rubric is wrong for the question the harness asks ("did the model answer correctly"),
  and the evidence for that is solid. My replacement is a first draft that made every judge more lenient and
  helped only one of three. **I recommended "luna with the split rubric" in the first version of this report;
  the table does not support it**, and the best-measured combination is Haiku with the split rubric, by a margin
  that is not significant. The rubric needs a controlled test: one change at a time, more truth-scored answers.
- **Answer key.** The strongest result needs no judge at all: CUAD's annotations can score seven of ten ask
  questions directly, they are the only thing here that sees recall, and they cost nothing per run. Extending
  that key is also the cheapest way to get the larger truth set that every open question above is waiting on.
- **Second judge.** Replacing `gpt-4o` (the audit's `--cross-judge`) was not tested: it was not in the bake-off,
  and that path uses `answer_judge`'s rubric with an answer key, not either rubric here. The only thing
  established is price: `gpt-4o` costs more than `gpt-5.6-terra`, and its cached reads cost six times as much.
  Two cheap judges from two vendors would cost about half a cent per answer, under half of today's one.
- **Baseline model.** Not tested. It is 96% of a run's cloud cost (#682); the levers in order are the prompt
  caching from #693, which has not been measured, reusing baselines across runs of an unchanged corpus, and only
  then a cheaper model, which is a weaker reference.
- **Timing.** No valid baseline exists to stay comparable with, so a change of judge or rubric before the
  re-baseline costs nothing in comparability. After it, every such change restarts the series.

## Not established

- Any quality ordering of the judges. Seven questions, three models, one corpus, one evening.
- Anything about the `gpt-5.6` models beyond their price page and these 250 verdicts: not their deprecation
  schedule, not how they judge long research reports (the longest answers here are a few thousand characters),
  not other languages. Enron and the synthetic corpus were not judged.
- Sonnet 5 and `gpt-5.6-terra` were not repeated, so their self-consistency is unmeasured. Sonnet 5 ran with
  thinking off, to compare one deployment with the others; a judge that thinks is a different candidate.
- The split rubric: see above. Its marginal band is loose (three judges agree on pass-versus-not for 45 to 49
  of 50 answers, on the exact verdict for 35 to 46).
