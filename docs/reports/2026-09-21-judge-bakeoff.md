# Judge bake-off, 2026-09-21

Asked by the owner before the re-baseline (#549): is the judge model (`claude-sonnet-4-6`, also the baseline
model) worth replacing with something newer, smaller or cheaper, and what about OpenAI's current prices? Answers
#661. Cloud spend: **USD 2.93** of a 3.00 cap, 500 verdicts.

## What was done

`scripts/test_corpora/judge_bakeoff.py` (new, in this PR) re-judged 50 answers that run `bench-20260921-0041`
left on disk: every non-empty answer from `gemma4-26b-a4b`, `gpt-oss-20b` and `qwen3-8b` to 16 CUAD questions,
plus two of `qwen36-35b-a3b`'s canned failures as a sanity check. Five judges saw identical prompts: the sweep's
own rubric, and a **split rubric** that scores "does it answer the question" apart from "does it cover the
reference" and takes the verdict from the first. That run's retrieval was broken (#685), which does not matter
here: the answers span the whole range of quality, which is what calibrating a judge needs.

**Ground truth is human, not mine.** CUAD ships `master_clauses.csv`, expert annotations for 41 clause types per
contract. Seven of the ten "ask" questions map onto it directly: governing law, parties and term for the
single-contract questions; California law, most-favoured-nation, anti-assignment and exclusivity for the "list
contracts with" questions, computed over the 81 contracts ingested. Each answer got a truth score from 0 to 1
(the annotated fact is present; or F1 of the contracts it names against the labelled set). That gives **23 answers
with a known truth score**. It is a small number, and every interval below is wide because of it.

## Results

Prices are per verdict as metered (about 2,500 tokens in, 400 out).

| judge | USD per verdict | same verdict on a second pass | same verdict as Sonnet 4.6 | rank correlation with human truth (95% bootstrap) |
|---|---|---|---|---|
| `claude-sonnet-4-6` (today) | 0.0139 | 49 of 49 | | 0.79 [0.55, 0.92] and 0.68 [0.37, 0.89] |
| `claude-sonnet-5` (thinking off) | 0.0112 | not repeated | 45 of 49 | 0.78 [0.51, 0.95] |
| `claude-haiku-4-5` | 0.0043 | 50 of 50 | 44 of 49 | 0.71 [0.33, 0.94] and 0.65 [0.30, 0.90] |
| `gpt-5.6-terra` | 0.0087 | not repeated | 46 of 49 | 0.84 [0.64, 0.95] |
| `gpt-5.6-luna` | **0.0010** | 48 of 50 | 44 and 46 of 49 | 0.81 [0.56, 0.96] and 0.83 [0.61, 0.95] |

All 500 verdicts parsed as JSON. Every judge's completeness scores correlate 0.92 to 0.96 with Sonnet 4.6's.

**1. No judge is distinguishable from another on quality.** Every interval overlaps every other. The two OpenAI
models are nominally highest and the cheapest Claude nominally lowest, and with 23 answers that ordering is not
evidence. What is evidence: all five are self-consistent, they agree with each other, and `gpt-5.6-luna` does it
at a fourteenth of today's price. (Sonnet 5's list price is a third lower than Sonnet 4.6's; with its tokenizer
producing about 30% more tokens for the same text, it metered 20% cheaper.)

**2. The rubric matters far more than the model.** Seven of the 23 answers are fully right by the human
annotations, thirteen are wrong (truth under 0.2):

| rubric | judge | of 7 right answers, passed | of 13 wrong answers, failed |
|---|---|---|---|
| sweep's | `claude-sonnet-4-6`, `claude-sonnet-5`, `claude-haiku-4-5` | **1** each | 10, 10, 11 |
| sweep's | `gpt-5.6-terra` / `gpt-5.6-luna` | 2 / 4 | 10 / 10 |
| split | `claude-haiku-4-5` | **6** | 10 |
| split | `gpt-5.6-terra` / `gpt-5.6-luna` | 5 / 5 | 7 / 8 |

The sweep's rubric grades coverage of the baseline's territory, so an answer that is right and short is
"marginal": right answers average 2.2 to 3.2 of 5 under it, and 4.8 to 5.0 under the split rubric. This is why
the last report showed one pass in sixteen for every model. Under the split rubric the three judges agree on
pass-versus-not for 45 to 49 of 50 answers, and less on marginal-versus-fail (35 to 46 of 50): the middle band
needs anchoring before its counts mean much.

**3. The reference is fallible, and a faithful judge inherits its errors.** Scored against the same human
annotations, the Claude baseline got the three single-contract facts right, and on the list questions reached
F1 0.72 (California law), 0.50 (anti-assignment: 23 of 65 named), 0.13 (exclusivity: 3 of 40) and **0.00 on
most-favoured-nation, where it stated that no contract in the corpus has such a clause and the annotators
labelled five**. Two local models out-scored the baseline on three of the seven questions. That run's broken
retrieval understates what the baseline can do, but the structure stands: a judge cannot know what the reference
missed, and marks a model down for finding it. Some of the "wrong answers passed" above are this in reverse: a
model that names three correct anti-assignment contracts out of 65 looks fine to any judge.

**4. Found in passing: `citation_overlap` is inflated.** A baseline's `cited_doc_titles` is every document any of
its tool calls returned, not what its answer cites (51 titles behind a 636-character answer that cites none), and
the sweep scores a model's citations against that list.

## Reading

- **Judge:** move to `gpt-5.6-luna` with the split rubric, and keep `claude-haiku-4-5` as the second judge in
  place of `gpt-4o` (which now costs more than `gpt-5.6-terra`, with cached reads six times dearer). Two cheap
  judges from two vendors on every answer cost about half a cent, well under half of today's single judge, and where they
  disagree is the list a person should read. A judge from another vendor than the baseline is also the simplest
  answer to same-family preference, which this data hints at (finding 2, first two rows) and cannot establish.
- **Answer key:** for CUAD, score against the human annotations wherever they apply and use the judge only for
  what they do not cover (the research questions). It is the only thing here that can see recall, it costs
  nothing per run, and it removes most of the reason to pay for a baseline on those questions.
- **Baseline model:** not tested here. It is 96% of a run's cloud cost (#682), and the levers in order are the
  prompt caching that landed in #693 and has not been measured, reusing baselines across runs of an unchanged
  corpus, and only then a cheaper model, which is a weaker reference.
- **Now is the cheap moment:** no valid baseline exists to stay comparable with, so changing judge and rubric
  before the re-baseline costs nothing in comparability. After it, every such change restarts the series.

## Not established

- 23 truth-scored answers, one corpus, one evening. The model-to-model ordering is noise.
- I know nothing about the `gpt-5.6` models beyond their price page and these 250 verdicts: not their
  deprecation schedule, not how they judge long research reports (the longest answers here are a few thousand
  characters), not other languages. Enron and the synthetic corpus were not judged.
- Sonnet 5 was run with thinking off, to compare one deployment with the others. A judge that thinks is a
  different candidate.
- My truth score for list questions is F1 over contract names matched in the answer text and its citations. It
  is strict on recall by design and crude about names; a contract referred to only by a paraphrase is missed.
- The split rubric is a first draft that three judges happened to apply similarly. It has not been tuned, and
  its marginal band is loose.
- The verdict files and the analysis scripts are on the mini (the run's `bakeoff/` directory), not in the repo.
  Only the tool that produced the verdicts is in this PR.
