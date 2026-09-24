# Benchmark run `bench-20260924-0724-keyed` on ix, 2026-09-24

- Suite commit: `67dd3e5`
- Run started: 2026-09-24T11:24:56Z
- Corpora: cuad
- Cloud spend: USD 4.71 of a 25.00 cap over 147 calls; judge gpt-5.6-luna; prices as of 2026-09-21.
- Machine preflight: **warn** at 2026-09-24T11:24:37Z, llama.cpp pin v0.4.1.
  - warn: swap in use: 2789 MB

## Phase 4: every model, every question

| corpus | model | planned | ran | done | degraded | error | citation overlap | entity overlap | median latency (s) | judged | pass | marginal | fail | answers question (0-5) | completeness (0-5) | answer key (0-1) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cuad | `gemma4-26b-a4b` | 20 | 20 | 20 | 0 | 0 | 0.12 | 0.04 | 20 | 20 | 3 | 2 | 15 | 1.25 | 1.15 | 0.24 |
| cuad | `gpt-oss-20b` | 20 | 20 | 20 | 0 | 0 | 0.59 | 0.09 | 57 | 20 | 15 | 4 | 1 | 4.45 | 3.45 | 0.89 |
| cuad | `qwen3-8b` | 20 | 20 | 20 | 0 | 0 | 0.40 | 0.16 | 86 | 20 | 8 | 1 | 11 | 2.40 | 2.15 | 0.60 |
| cuad | `qwen36-35b-a3b` | 20 | 20 | 20 | 0 | 0 | 0.65 | 0.36 | 49 | 20 | 12 | 1 | 7 | 3.15 | 2.90 | 0.65 |

The overlap means include units whose baseline was unusable: the sweep records those as 0.000, and metrics.csv does not tell them from a true zero. `log.txt` names each one.

## Cloud spend

| call kind | units | calls | input tokens | cache write | cache read | output tokens | USD |
|---|---|---|---|---|---|---|---|
| baseline_question | 20 | 67 | 107 | 631120 | 1713127 | 23049 | 4.6467 |
| judge | 80 | 80 | 100509 | 0 | 0 | 36266 | 0.0636 |

## Reading

**The keyed CUAD run: 20 questions with a human answer key (8 governing-law facts, 6 dates, 6 lists of contracts), all four curated models, build `67dd3e5`, the machine at rest before the start (preflight warn: 2.8 GB of swap, everything else passing).** All 80 units ran and finished; no error, no restart, no stalled unit. The "answer key (0-1)" column is what this run exists for: it scores each answer against the annotations with no judge and no baseline, so it sees recall, and it is the one column that does not depend on what Claude wrote.

### The answer key, by kind

| model | fact (8) | date (6) | list (6) | all 20 |
|---|---|---|---|---|
| Claude baseline (`claude-sonnet-4-6`, scored offline with the same scorer) | 1.00 | 1.00 | 0.64 | 0.89 |
| `gpt-oss-20b` | 1.00 | 1.00 | 0.62 | **0.89** |
| `qwen36-35b-a3b` at 16,384 | 0.88 | 1.00 | 0.00 | 0.65 |
| `qwen3-8b` | 0.62 | 0.67 | 0.49 | 0.60 |
| `gemma4-26b-a4b` | 0.12 | 0.17 | 0.46 | 0.24 |

The sweep does not key-score phase 1, so the baseline row was computed after the run from `baselines/cuad/*.json` with `answer_key.score`; it is the same function on the same 20 keys.

### What the numbers support

- **`gpt-oss-20b` matches the cloud baseline on this key.** 1.00 on every fact and date, 0.62 on lists against Claude's 0.64, 0.89 overall against 0.89. The judge agrees: 15 pass, 4 marginal, 1 fail, answers-question 4.45 of 5. It is the slowest of the four on lists (two units over 600 s) and the one that never returned a canned answer.
- **Qwen3.6 at 16,384 tokens answers single-contract questions and cannot do lists at all.** 14 of 14 facts and dates but one scored 1.00. All six list answers are the app's fallback sentence: `api.log` shows "Context budget 123% >= 75% after 2 tool rounds, forcing text response" for each, the forced final answer came back empty, and chat returned "I used the available context to search but wasn't able to formulate a complete response" with status `completed`. The harness recorded six `done` units and the judge marked them fail; the key scored them 0, which is the true value of what the user received, but the row's 0.00 on lists is the 16K context, not the model: in the rubric test at 73,728 tokens this model scored 0.84 on the same six lists. Filed as #712; the fallback-as-success part is #684 item 3.
- **Gemma 4 26B-A4B does not look for the contract.** 12 of its 14 fact and date answers are "I cannot find a document titled "…" in the knowledge base": it searches once for the display name the question uses, gets no exact hit on a CUAD filing name, and stops. The other three models found every one of those contracts through the same tools. Its list answers are real attempts (0.46, with 0.92 on `cuad-key-20`). This is the behaviour the rubric test saw on 8 of 8 governing-law questions, now at 12 of 14; filed as #711. It is the same model that did best at Research the day before (#710), where the pipeline plans the searches for it.
- **`qwen3-8b` is in the middle and honest about it.** 9 of 20 at 1.00, 6 at 0; no canned answers; the judge's 8 pass agrees with the key's 9.
- **Judge and key agree on 79 of 80 answers** at the level of pass-versus-zero. The one disagreement is the key's: `qwen3-8b` on `cuad-key-7` said the governing law was not stated and mentioned Nevada as a party's state of incorporation; the term matched and the key gave 1.0; the judge gave fail, correctly. Filed as #713; the row's fact figure of 0.62 includes that 1.0 and would be 0.50 without it.

### What the numbers do not support

- A verdict on Qwen3.6 as a model: at 16K it is what a 32 GB Mac gets since #698, and that is what the row measures. On a 36 GB or larger Mac the list column would be a different number (0.84 at 73K in the rubric test, on the same six questions).
- Anything about the three corpora without a key, or about research-type questions: the keyed set is single-hop facts, dates and lists.
- Fine ranking on lists: six questions, partial-credit F1, and the baseline itself scores 0.31 to 1.00 on them.

### Cost

USD 4.71: 20 Claude baselines for USD 4.65 (67 calls, 1.7 M cache-read tokens, 631 K cache-write, 23 K out; USD 0.23 a question against #708's estimate of USD 0.50), and 80 verdicts for USD 0.06.

### Not verified

- Whether Gemma would find the contract if the question used the filing name; the rubric test's hypothesis (display name versus filing name) fits every one of the 12 but was not tested by asking differently.
- Whether the six empty forced answers were the model producing nothing or llama-server rejecting a prompt over its context; `api.log` shows no error and `llm.log` no OOM, so the first is likelier.
- `qwen3-4b`, the Qwen3.5 pair and Gemma 4 12B: not downloaded on this machine, not run.

### Where this leaves the four models

With #702 (the judged run), #710 (the two big models after the fix) and this, all four curated models have a judged row and a keyed row on CUAD on a healthy index. The keyed column changes the story the judged rows told: `gpt-oss-20b` is the model that gets the facts, Gemma is the model that does research well and single-contract lookup badly for a reason that is fixable in the prompt or the search tool (#711), and Qwen3.6 on a 32 GB Mac is a good single-question model with no room for lists (#712). Registry changes that follow are a separate PR.
