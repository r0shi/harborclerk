# Offline rescore of every stored run under the #816 list rule, 2026-10-17

- Scorer before: `scripts/test_corpora/runner/answer_key.py` at `c2614ff` (main, with #814's exclusion-section rule)
- Scorer after: the same file on `fix/816-list-scorer-negation`, third version (label rule only, after two reviews)
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

Only labels are read. A heading or bold lead-in that rejects takes its section out, as under #814. A line or bullet
that opens with a rejecting label naming no item ("- **Excluded near-misses:** … `0050` …", "Not counted: X") loses
the rest of its line. A label rejects only when:
- it **opens** with a reject phrase ("excluded", "exclusions", "not counted", "not included", "not a match",
  "non-match", "near-miss", "false positive", "rejected", "ruled out", "does not qualify", and the French) or with a
  denial of the category ("Not a non-disparagement clause"), after markup, emoji and numbering; or
- its **leading clause**, the words before the first parenthesis, comma, colon, semicolon or dash, **ends** with a
  reject phrase ("Reviewed and excluded", "Context on what was excluded").

Not when the leading clause opens with a negation ("No contracts were excluded", "Nothing has been excluded") or a
condition ("If the first offer is rejected"), when "excluded from" names something other than the list ("Excluded
from the cap"), or when the denial is of a degree or form or goes on ("Not a classic covenant", "Not a party but
bound"). A reject word anywhere else in a label does nothing, hedges count, and prose is not read for rejection.

Why so little: a false drop silently lowers recall on a right answer; a false keep only lowers precision, which the
reports read as a floor for narrating models. Two earlier versions read negation in headings and verdicts in prose;
each review found true matches dropped on plausible answers, and across the 154 stored answers the prose rule made
two drops. Every probe from both reviews is now a test with its outcome under this rule.

The leading-clause clause is mine, on top of the coordinator's "begins with": without it synthetic-key-20's
"**Context on what was excluded:**" keeps Skylight and Northstar (0.50), which breaks a named case, and Fable's
"## Reviewed and excluded" sections would keep their items. Its cost, pinned in a test: a leading clause ending in a
participle about something else ("Clauses with consequential damages excluded") rejects. A reject word after the
leading clause does nothing ("(limits not listed)", "(… the offer is rejected)", "(employees excluded)").

## Before and after

Cloud subjects (phase-1 baselines), from `cloud_subject_table`:

| run | model | corpus | answer key, before → after | lists, before → after |
|---|---|---|---|---|
| rubric-20260921-1754 | claude-sonnet-4-6 | cuad | 0.92 → 0.92 | 0.72 → 0.74 |
| bench-20260924-0724-keyed | claude-sonnet-4-6 | cuad | 0.91 → 0.91 | 0.72 → 0.72 |
| bench-20260924-2233-keyed-gemma | claude-sonnet-4-6 | cuad | 0.91 → 0.91 | 0.72 → 0.72 |
| bench-20260928-1050-keyed | claude-sonnet-4-6 | cuad | 0.93 → 0.93 | 0.76 → 0.76 |
| bench-20261014-2328-cuad-fable | claude-fable-5-1 | cuad | 0.95 → 0.95 | 0.82 → 0.82 |
| bench-20261015-0011-cuad-astra | gpt-6-astra | cuad | 0.96 → 0.96 | 0.87 → 0.86 |
| bench-20261015-0032-synthetic-fable | claude-fable-5-1 | synthetic | 1.00 → 1.00 | 0.99 → 1.00 |
| bench-20261015-0106-synthetic-astra | gpt-6-astra | synthetic | 1.00 → 1.00 | 1.00 → 1.00 |
| bench-20261015-2259-cuad-fable-rerun | claude-fable-5-1 | cuad | 0.95 → 0.96 | 0.84 → 0.88 |

Local models (phase 4), from `model_table`, in bench-20260924-0724-keyed, bench-20260924-2233-keyed-gemma,
bench-20260928-1050-keyed and rubric-20260921-1754: **no unit changed.**

## Every item whose credit changed

15 items in 6 answers. "Right" is by the answer's plain meaning, read in full.

| # | run, model, question | item (key) | change | caused by | right? |
|---|---|---|---|---|---|
| 1 | bench-20261015-0032-synthetic-fable, fable, synthetic-key-8 | 0050 (negative) | dropped | line label "- **Excluded near-misses:** invoices *due* in February but issued in January — … `0050` (GS-2025-00847, Jan 3) … — were not included" | yes. 0.96 → 1.00 |
| 2 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | eDiets (negative) | dropped | heading "Not a non-disparagement clause (word appears only incidentally)" | yes |
| 3–5 | rubric-20260921-1754, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)": leading clause ends "Do NOT Qualify" | yes. 0.40 → 0.50 |
| 6 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | Cybergy (positive) | counted | heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)" | yes: "Functionally a non-disparagement obligation"; the summary says it "arguably qualifies" |
| 7–10 | same answer | Berkshire Hills, Digital Cinema (positives); Creditcards.com, Rand (negatives) | counted | the same Borderline heading | by the decided rule, yes; by plain meaning, doubtful: offered as related provisions, and the summary calls the four "mark-usage restrictions or termination triggers rather than general non-disparagement covenants". 0.43 → 0.67 for 2–10 together |
| 11 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-19 | Prudential (negative) | counted | heading "Borderline / special case" | **no**: "an insurance arrangement itself rather than a covenant to carry liability coverage, so I list it separately"; the answer's count, 22, leaves it out. 1.00 → 0.98 |
| 12 | bench-20261014-2328-cuad-fable, fable, cuad-key-18 | Coherus (negative) | counted | heading "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)" | by the decided rule, yes; by plain meaning, a hedge: "not labeled as, and weaker than, a formal ROFN" |
| 13 | same answer | Revolution Medicines (positive) | counted | the same heading | by the decided rule, yes; by plain meaning, **no**: "a priority between the parties to deal with third parties, not a ROFR/ROFN". The key labels it positive |
| 14 | same answer | El Pollo Loco (negative) | counted | the same heading | by the decided rule, yes; by plain meaning, **no**: "the Development Agreement doesn't itself set out the ROFR". Score unchanged, 0.667 |
| 15 | bench-20261015-0011-cuad-astra, gpt-6-astra, cuad-key-16 | eDiets (negative) | counted | lead-in "**One additional keyword match, not counted above:**": the reject phrase comes after the leading clause | **no**: the answer says it is not counted. 0.667 → 0.632 |

All 5 drops are of contracts the key does not label, each rejected by the answer's own label. Of the 10 additions,
4 are positives. Items 11, 13, 14 and 15 count a contract the answer says is not a match; 7–10 and 12 count ones it
offers as weaker or related. Items 11–14 are the cost of reading every hedge as offered; item 15 is the cost of
reading only a label's leading clause.

### What the earlier versions dropped that this one keeps

- RaeSystems, Sonnet 4.6, bench-20260928-1050-keyed cuad-key-17: "…only has *revocable* licenses, so I'll exclude
  it." Prose, not read. 0.952, as on main (the second version gave 1.000). Pinned as an accepted false keep.
- The first version's 14 further drops (Sonnet "(NOT Customer Solicitation Bans)", qwen35-4b "Not Found:", qwen35-9b
  "but these are … rather than", qwen36-35b-a3b "they do not appear to contain", qwen38-27b "(Not Customers)"), all
  of unlabelled contracts the answers rejected, are kept as on main.

## The three cases #816 names

- **synthetic-key-20** (bench-20261015-0032-synthetic-fable): Skylight and Northstar are not counted, 1.00, as on
  main: "**Context on what was excluded:**" rejects (its leading clause ends with "excluded"). The issue's 0.50
  predates #814. Without that lead-in both would count: the prose after it is not read (pinned).
- **synthetic-key-8** (same run): the extra contract was 0050, not 0048. "0048 is a July 2025 invoice — excluded"
  names 0048 by no form the key holds; 0050 was named by its invoice number under a bulleted bold label, which #814
  did not read and the line-label rule does: 0.96 → 1.00. Had 0048 been named by its stem in that prose line, it
  would be counted (pinned as a cost).
- **cuad-key-16** (bench-20261015-2259-cuad-fable-rerun): Berkshire Hills, Cybergy and Digital Cinema are counted,
  eDiets is not: 0.43 → 0.67 (P 0.67, R 0.67), equal to the first run, which is unchanged. The same answer with its
  table written as bullets names the same nine contracts (a review probe, now a test with the absolute set).

## What this does not show

- That labels capture what answers reject. They capture what an answer marks with an explicit label; anything said
  in prose, or after a label's leading clause, is counted. On these runs that is precision lost by the narrating
  cloud subjects, which the reports already read as a floor.
- Anything about the French phrases on real data: no stored list answer is in French with a rejecting label.
