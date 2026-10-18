# Offline rescore of every stored run under the #816 list rule, 2026-10-17

- Scorer before: `scripts/test_corpora/runner/answer_key.py` at `c2614ff` (main, with #814's exclusion-section rule)
- Scorer after: the same file on `fix/816-list-scorer-negation`
- Machine: ix. No instance, no model and no cloud call: USD 0.
- Runs: every directory under the eval workdir's `results/` (20). Nothing there was written.
- Keys: the committed `questions/keyed/{cuad,synthetic}.key.json`, for both scorers, so a difference is the rule's
  alone. A September run was scored at the time with the key as it was then, so these numbers need not match the
  ones in its own report.
- Method: the cloud-subject rows come from `report.py`'s `cloud_subject_table` and the phase-4 rows from its
  `model_table` over cells recomputed by `sweep.py`'s `key_score_cell`, each run once with the old module and once
  with the new one swapped in. The new scorer is given each question's text as the scoring sites now pass it: a
  baseline's recorded `question`, and for a phase-4 response the text in `questions/keyed/*.yaml`. Item-level
  differences come from `named_contracts` with the `doc_ids` map each scoring site builds. 154 stored answers to
  keyed list questions were compared; every changed item below was read in its answer.

## The rule

Only labels are read for rejection: headings, bold lead-ins, and the bold or colon-terminated label that opens a
line or bullet. A heading or lead-in that rejects takes its section out, as under #814; a line label that rejects
and names no item takes the rest of its line. A label rejects when:

- it **opens** with a reject phrase: excluded, exclusions, not counted, not included, not a match, non-match,
  near-miss, false positive, rejected, ruled out, does not qualify, and the French equivalents; or
- it **opens** with a denial of the question's own category: "Not a non-disparagement clause" against "Which
  contracts contain a non-disparagement clause?" (the denied phrase's last content word is one of the question's).
  Without the question, no denial is read; or
- its **leading clause**, the words before the first parenthesis, comma, colon, semicolon or dash, **ends** with a
  reject phrase: "Reviewed and excluded", "Context on what was excluded".

It does not reject when:
- the leading clause opens with a negation ("No contracts were excluded", "Nothing has been excluded");
- a closing phrase is attached to something else by a condition or attachment word in the leading clause (once,
  after, when, whenever, if, unless, until, provided, with, without, where, except, and the French): "Third-party
  sale once the first offer is rejected", "Customer non-solicits with employees excluded";
- the words right after the label say it rejects nothing ("Exclusions: none; Staar and Papa John's both qualify");
- an opening participle describes something else: "Excluded from the cap" (but "Excluded from the list /
  consideration / scope / review / tally" rejects), "Rejected offers go to third parties", "Rejected-offer matching
  rights";
- a denial denies a degree or a form ("Not a classic covenant"), names another category ("Not a ROFR" against a
  question about first refusal, offer or negotiation; "Not a Delaware entity" against one about Delaware law), or
  goes on to say what the item is instead ("Not a right of first refusal — a right of first offer").

A reject word anywhere else in a label does nothing, hedges count ("Borderline / related", "Mentions / borderline"),
and prose is not read for rejection.

Why so little: a false drop silently lowers recall on a right answer, while a false keep lowers only precision,
which the reports read as a floor for narrating models. Rules that read negation anywhere in a heading, or verdicts
in prose, drop true matches on plausible answers: the clauses asked for are themselves prohibitions ("Irrevocable
licenses (may not be revoked)"), and descriptions of a clause use the same words as verdicts ("if the offer is
rejected, STAAR may sell"). Across the 154 stored answers, a prose rule would have made two drops.

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
| 2 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | eDiets (negative) | dropped | heading "Not a non-disparagement clause (word appears only incidentally)", a denial of the question's category | yes |
| 3–5 | rubric-20260921-1754, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)": the leading clause ends "Do NOT Qualify" | yes. 0.40 → 0.50 |
| 6 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | Cybergy (positive) | counted | heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)" | yes: "Functionally a non-disparagement obligation"; the summary says it "arguably qualifies" |
| 7–10 | same answer | Berkshire Hills, Digital Cinema (positives); Creditcards.com, Rand (negatives) | counted | the same Borderline heading | by the rule, yes (hedges count); by plain meaning, doubtful: offered as related provisions, and the summary calls the four "mark-usage restrictions or termination triggers rather than general non-disparagement covenants". 0.43 → 0.67 for 2–10 together |
| 11 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-19 | Prudential (negative) | counted | heading "Borderline / special case" | **no**: "an insurance arrangement itself rather than a covenant to carry liability coverage, so I list it separately"; the answer's count, 22, leaves it out. 1.00 → 0.98 |
| 12 | bench-20261014-2328-cuad-fable, fable, cuad-key-18 | Coherus (negative) | counted | heading "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)" | by the rule, yes; by plain meaning, a hedge: "not labeled as, and weaker than, a formal ROFN" |
| 13 | same answer | Revolution Medicines (positive) | counted | the same heading | by the rule, yes; by plain meaning, **no**: "a priority between the parties to deal with third parties, not a ROFR/ROFN". The key labels it positive |
| 14 | same answer | El Pollo Loco (negative) | counted | the same heading | by the rule, yes; by plain meaning, **no**: "the Development Agreement doesn't itself set out the ROFR". Score unchanged, 0.667 |
| 15 | bench-20261015-0011-cuad-astra, gpt-6-astra, cuad-key-16 | eDiets (negative) | counted | lead-in "**One additional keyword match, not counted above:**": the reject phrase comes after the leading clause | **no**: the answer says it is not counted. 0.667 → 0.632 |

All 5 drops are of contracts the key does not label, each rejected by the answer's own label. Of the 10 additions,
4 are positives. Items 11, 13, 14 and 15 count a contract the answer says is not a match; 7–10 and 12 count ones it
offers as weaker or related. Items 11–14 are the cost of reading every hedge as offered, and item 15 the cost of
reading only a label's opening and its leading clause.

Rejections said in prose stay counted. In the stored answers that includes RaeSystems (Sonnet 4.6,
bench-20260928-1050-keyed, cuad-key-17: "…only has *revocable* licenses, so I'll exclude it."), which scores 0.952 as
on main, and the contracts local models rejected with a negation ("(NOT Customer Solicitation Bans)", "Not Found:",
"they do not appear to contain …"), all unlabelled.

## The three cases #816 names

- **synthetic-key-20** (bench-20261015-0032-synthetic-fable): Skylight and Northstar are not counted, 1.00, as on
  main: "**Context on what was excluded:**" rejects, its leading clause ending with "excluded". The issue's 0.50
  predates #814. Without that lead-in both would count, since the prose after it is not read (pinned).
- **synthetic-key-8** (same run): the extra contract was 0050, not 0048. "0048 is a July 2025 invoice — excluded"
  names 0048 by no form the key holds; 0050 was named by its invoice number under a bulleted bold label, which #814
  did not read and the line-label rule does: 0.96 → 1.00. Had 0048 been named by its stem in that prose line, it
  would be counted (pinned as a cost).
- **cuad-key-16** (bench-20261015-2259-cuad-fable-rerun): Berkshire Hills, Cybergy and Digital Cinema are counted,
  eDiets is not: 0.43 → 0.67 (P 0.67, R 0.67), equal to the first run, which is unchanged. The same answer with its
  table written as bullets names the same nine contracts.

## What this does not show

- That labels capture what answers reject. They capture what an answer marks with an explicit label at a label's
  start or at the end of its leading clause; anything said in prose, or later in a label, is counted. On these runs
  that is precision lost by the narrating cloud subjects, which the reports already read as a floor.
- Anything about the French phrases on real data: no stored list answer is in French with a rejecting label.
