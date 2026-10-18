# Offline rescore of every stored run under the #816 list rule, 2026-10-17

- Scorer before: `scripts/test_corpora/runner/answer_key.py` at `c2614ff` (main, with #814's exclusion-section rule)
- Scorer after: the same file on `fix/816-list-scorer-negation`, second version (after review)
- Machine: ix. No instance, no model and no cloud call: USD 0.
- Runs: every directory under the eval workdir's `results/` (20). Nothing there was written.
- Keys: the committed `questions/keyed/{cuad,synthetic}.key.json`, for both scorers, so a difference is the rule's
  alone. A September run was scored at the time with the key as it was then, so these numbers need not match the
  ones in its own report.
- Method: the cloud-subject rows come from `report.py`'s `cloud_subject_table` and the phase-4 rows from its
  `model_table` over cells recomputed by `sweep.py`'s `key_score_cell`, each run once with the old module and once
  with the new one swapped in. Item-level differences come from `named_contracts` with the `doc_ids` map each
  scoring site builds. 154 stored answers to keyed list questions were compared; every changed item below was
  read in its answer.

## The rule

A false drop silently lowers recall on a right answer; a false keep only lowers precision, which the reports already
read as a floor for a narrating model. So an item is dropped only on a rejection that is explicit and attached to
it. The module docstring has the rule in full.

**Headings.** A heading or bold lead-in rejects its section only on an explicit reject phrase ("excluded", "not
counted", "not a match", "non-match", "near-miss", "false positive", "rejected", "does not qualify", "ruled out", and
the French) that no negation just before it cancels ("Nothing excluded", "No false positives"), or when it opens by
denying the category ("Not a non-disparagement clause"). Hedges count ("Borderline / related", "Mentions /
borderline"), and so does any other negation, because the clauses asked for are prohibitions ("may not be revoked",
"shall not disparage", "(not mutual)").

**Prose.** A mention is dropped only on a verdict attached to that one name in its sentence: "<name> is not a match /
is not one / does not qualify / is excluded / — excluded / (excluded) / is not an <other category>", "excluded <name>",
"Not counted: <name>", "rather than <name>", "<name>, not <other>"; "<name> … so I'll exclude it" and a sentence-final
"— excluded" when the sentence names one item. A sentence opening with a "Label:" that names no item and rejects is
read as a heading over that sentence. A negation before a verdict cancels it; quotations are not read; a verdict
never reaches back over a clause, a colon or a contrast word. A bare negated predicate ("does not target", "is not
specified") is not a verdict. Sentences now also end before a backtick or a lower-case word after a full stop.

The first version (reviewed, not merged) read any unqualified negation in a heading, and any cue within ten words in
prose, as a rejection. The review found it dropped true matches on plausible answers ("Irrevocable licenses (may not
be revoked)", "if the offer is rejected, STAAR may sell…", "Staar matches, eDiets is not a match" dropping Staar).
Every review probe is now a test with its outcome.

## Before and after

Cloud subjects (phase-1 baselines), from `cloud_subject_table`:

| run | model | corpus | answer key, before → after | lists, before → after |
|---|---|---|---|---|
| rubric-20260921-1754 | claude-sonnet-4-6 | cuad | 0.92 → 0.92 | 0.72 → 0.74 |
| bench-20260924-0724-keyed | claude-sonnet-4-6 | cuad | 0.91 → 0.91 | 0.72 → 0.72 |
| bench-20260924-2233-keyed-gemma | claude-sonnet-4-6 | cuad | 0.91 → 0.91 | 0.72 → 0.72 |
| bench-20260928-1050-keyed | claude-sonnet-4-6 | cuad | 0.93 → 0.93 | 0.76 → 0.77 |
| bench-20261014-2328-cuad-fable | claude-fable-5-1 | cuad | 0.95 → 0.95 | 0.82 → 0.82 |
| bench-20261015-0011-cuad-astra | gpt-6-astra | cuad | 0.96 → 0.96 | 0.87 → 0.87 |
| bench-20261015-0032-synthetic-fable | claude-fable-5-1 | synthetic | 1.00 → 1.00 | 0.99 → 1.00 |
| bench-20261015-0106-synthetic-astra | gpt-6-astra | synthetic | 1.00 → 1.00 | 1.00 → 1.00 |
| bench-20261015-2259-cuad-fable-rerun | claude-fable-5-1 | cuad | 0.95 → 0.96 | 0.84 → 0.88 |

Local models (phase 4), from `model_table`, in bench-20260924-0724-keyed, bench-20260924-2233-keyed-gemma,
bench-20260928-1050-keyed and rubric-20260921-1754: **no unit changed.** No local-model answer carries an explicit
reject phrase on a heading or a verdict attached to a name.

## Every item whose credit changed

15 items in 6 answers. "Right" is by the answer's plain meaning, read in full.

| # | run, model, question | item (key) | change | caused by | right? |
|---|---|---|---|---|---|
| 1 | bench-20260928-1050-keyed, sonnet-4-6, cuad-key-17 | RaeSystems (negative) | dropped | prose: "The RaeSystemsInc Co-Branding Agreement only has *revocable* licenses, so I'll exclude it." | yes |
| 2 | bench-20261015-0032-synthetic-fable, fable, synthetic-key-8 | 0050 (negative) | dropped | sentence label: "- **Excluded near-misses:** invoices *due* in February but issued in January — … `0050` (GS-2025-00847, Jan 3) … — were not included" | yes |
| 3 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | eDiets (negative) | dropped | heading "Not a non-disparagement clause (word appears only incidentally)" | yes |
| 4–6 | rubric-20260921-1754, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)" | yes |
| 7 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | Cybergy (positive) | counted | heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)" | yes: "Functionally a non-disparagement obligation", and the summary says it "arguably qualifies" |
| 8–11 | same answer | Berkshire Hills, Digital Cinema (positives); Creditcards.com, Rand (negatives) | counted | the same Borderline heading | by the decided rule, yes; by the answer's plain meaning, doubtful: offered as related provisions, and the summary calls the four "mark-usage restrictions or termination triggers rather than general non-disparagement covenants" |
| 12 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-19 | Prudential (negative) | counted | heading "Borderline / special case" | **no**: "an insurance arrangement itself rather than a covenant to carry liability coverage, so I list it separately"; the answer's own count, 22, leaves it out. 1.00 → 0.98 |
| 13 | bench-20261014-2328-cuad-fable, fable, cuad-key-18 | Coherus (negative) | counted | heading "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)" | by the decided rule, yes; by plain meaning, a hedge: "not labeled as, and weaker than, a formal ROFN" |
| 14 | same answer | Revolution Medicines (positive) | counted | the same heading | by the decided rule, yes; by plain meaning, **no**: "a priority between the parties to deal with third parties, not a ROFR/ROFN". The key labels it positive |
| 15 | same answer | El Pollo Loco (negative) | counted | the same heading | by the decided rule, yes; by plain meaning, **no**: "the Development Agreement doesn't itself set out the ROFR". Score unchanged, 0.667 (precision down, recall up) |

All 6 drops are of contracts the key does not label, each rejected in the answer's own words. Of the 9 additions, 4
are positives. Items 12, 14 and 15 count a contract the answer says is not a match; items 8–11 and 13 count ones it
offers as weaker or related. These are the cost of reading every hedge as offered: a heading vocabulary cannot tell
"listed separately, not counted" from "weaker but offered".

### What came back from the first version

Fourteen drops the first version made, all of contracts the key does not label and each one the answer rejected,
are kept by the stricter rule, so their precision returns to main's:

- Sonnet 4.6, cuad-key-15, bench-20260924-0724-keyed and bench-20260924-2233-keyed-gemma: Clickstream, Coherus and
  Phasebio twice (six items) under "⚠️ Contracts With Employee-Only Non-Solicitation Clauses (NOT Customer
  Solicitation Bans)". "NOT Customer Solicitation Bans" is a negation, not an explicit reject phrase. Both answers
  go back to 0.429 from 0.545.
- qwen35-4b, cuad-key-17: Legacy Education and Southern Star under "## Not Found:" and "The search did not find any
  contracts containing perpetual license grants in [both]". Back to 0.500 from 0.571.
- qwen35-9b, cuad-key-18: Stamps.com, Paperexchange and Impresse in "(e.g., …), but these are procedural dispute
  resolution mechanisms rather than substantive first-offer or first-refusal rights": the verdict reaches back over
  a contrast word. Back to 0.593 from 0.667.
- qwen36-35b-a3b, cuad-key-16: HF Enterprises and WaterNow in "(such as …), they do not appear to contain explicit
  non-disparagement language": a bare negated predicate, after a clause. Score 0.000 either way (named 3, not 1).
- qwen38-27b, cuad-key-15: Coherus under "**Contracts with Non-Solicitation Clauses Limited to Employees (Not
  Customers):**". Back to 0.462 from 0.500.

Every prose rejection the stricter rule makes in the 154 stored answers is one of items 1 and 2.

## The three cases #816 names

- **synthetic-key-20** (bench-20261015-0032-synthetic-fable): Skylight and Northstar are not counted, 1.00. Main
  already scores this 1.00: the issue's 0.50 predates #814, whose lead-in rule drops "**Context on what was
  excluded:**". The prose rule alone would not: "— none target Acme Corp" and "those don't target Acme Corp" are not
  verdicts it reads. Pinned as a stated cost.
- **synthetic-key-8** (same run): the extra contract was 0050, not 0048. "0048 is a July 2025 invoice — excluded"
  names 0048 by no form the key holds (its aliases are its total); 0050 was named by its unique invoice number in a
  bulleted bold label, which #814's lead-in rule does not read. Dropped by the sentence-label rule: 0.96 → 1.00. A
  test pins that the quoted line, with 0048 named by its stem, is read as a rejection (the sentence-final "—
  excluded" with one name).
- **cuad-key-16** (bench-20261015-2259-cuad-fable-rerun): Berkshire Hills, Cybergy and Digital Cinema are counted,
  eDiets is not: 0.43 → 0.67 (P 0.67, R 0.67). The first run (bench-20261014-2328-cuad-fable) scores 0.67 under both
  rules, so the two runs now agree on this question, as they did, at 0.63 each, with the exclusion rule off. The same
  answer with its table rewritten as bullets scores the same (a review probe, now a test).

## What this does not show

- That the rule reads rejection in general. It reads the phrasings in 154 answers from three cloud subjects and eight
  local models, and the tests pin each pattern and guard against made-up sentences. A new model's way of writing a
  rejection will often fall outside the patterns, and then it is kept: the rule is built to err that way.
- Anything about the French patterns on real data: no stored list answer is in French with a rejection in it.
