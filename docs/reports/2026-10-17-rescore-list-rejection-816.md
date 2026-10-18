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
  with the new one swapped in. Item-level differences come from `named_contracts` with the `doc_ids` map each
  scoring site builds. 154 stored answers to keyed list questions were compared; every changed item below was read
  in its answer.

## What the rule does

Only section labels are read for rejection, as under #814: a heading, or a bold lead-in at the start of a line. A
label that rejects takes its section out, a heading's to the next heading of its level or higher and a lead-in's to
the next heading or line that opens bold (main's rule, unchanged); the answer's first section is never dropped. What
changes is when a label rejects.

A label rejects when:
- it **opens** with a reject phrase: excluded, not counted, not included, not a match, non-match, near-miss, false
  positive, rejected, ruled out, does not qualify, and the French equivalents; or
- its **leading clause**, the words before the first parenthesis, comma, colon, semicolon or dash, **ends** with a
  reject phrase after nothing but words for the items and framing words: "Reviewed and excluded", "Context on what
  was excluded", "Notable Contracts That Were Close But Do NOT Qualify".

A label does not reject when:
- a reject word comes anywhere else in it ("Insurance required (limits not listed)", "One additional keyword match,
  not counted above");
- the words before a closing reject phrase describe something else ("Punitive damages excluded", "No contracts were
  excluded");
- an opening participle describes something else ("Excluded from the cap", "Rejected offers go to third parties",
  "Rejected-offer matching rights", "Excluded from the total liability cap"), unless what it names is the list
  ("Excluded from the (final) list / consideration / tally", "Excluded from the count (above)", "Excluded due to
  scope", "Rejected for lack of a covenant");
- after a contrast in its leading clause, it says the item qualifies with no negation ("Not counted above, but
  qualifies"); a contrast past the leading clause ("Excluded (but the keyword matches)") or one that says only that
  a clause applies or a keyword matches does not count;
- what follows it, after its own colon or on the next line, is only "none", "nothing", "n/a", "zero" or the like,
  alone or before punctuation, a dash (a spaced hyphen counts) or a conjunction ("## Excluded: none", "**Excluded:**
  none - both qualify"; not "None of these contains …", not "Zero-rated:");
- it is a hedge ("Borderline / related", "Mentions / borderline (… not operative ROFRs …)");
- it denies a category ("Not a non-disparagement clause").

A lead-in is judged on its whole label: the bold words when a colon or a dash follows them ("**Excluded** — Papa"),
else the bold words and any words after them up to a colon ("**Excluded** from the cap: …" is the label "Excluded
from the cap"). A bold span followed by running words with no colon is a sentence, not a label, and a bulleted or
indented bold line is not a lead-in; either still ends the section above it, as on main. A spaced hyphen counts as a
dash ("**Excluded** - Papa"); an unspaced one does not. "Exclusions" may frame a closing phrase ("Exclusions and
near-misses") but does not reject on its own. "Exclusions" is not a reject phrase:
policy and liability exclusions are ordinary content.

## What the rule does not do

It does not read anything but section labels:
- not a label opening a line or bullet ("- **Excluded near-misses:** … 0050 …");
- not a line that is only a label above a list ("Excluded:" then bullets);
- not prose ("so I'll exclude it");
- not a denial of the question's category.

Each of these was tried in a reviewed version of this change, and each review found true matches dropped on
plausible answers. The clauses asked for are themselves prohibitions ("Irrevocable licenses (may not be revoked)").
A clause's description uses a verdict's words ("if the offer is rejected, STAAR may sell", "Exclusions: Papa John's
policy excludes punitive damages"). And a denial names a subtype as readily as the category ("Not a New York law
contract (Delaware governs …)"). Across the 154 stored answers, all of them together made three drops: synthetic
0050 by a bulleted label, the rerun's eDiets by a denial, and RaeSystems in prose. All three are now counted, as on
main.

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
| bench-20261015-0032-synthetic-fable | claude-fable-5-1 | synthetic | 1.00 → 1.00 | 0.99 → 0.99 |
| bench-20261015-0106-synthetic-astra | gpt-6-astra | synthetic | 1.00 → 1.00 | 1.00 → 1.00 |
| bench-20261015-2259-cuad-fable-rerun | claude-fable-5-1 | cuad | 0.95 → 0.96 | 0.84 → 0.87 |

Local models (phase 4), from `model_table`, in bench-20260924-0724-keyed, bench-20260924-2233-keyed-gemma,
bench-20260928-1050-keyed and rubric-20260921-1754: **no unit changed.**

## Every item whose credit changed

13 items in 5 answers. "Right" is by the answer's plain meaning, read in full.

| # | run, model, question | item (key) | change | caused by | right? |
|---|---|---|---|---|---|
| 1–3 | rubric-20260921-1754, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)": the leading clause ends "Do NOT Qualify" after framing words only | yes. 0.40 → 0.50 |
| 4 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | Cybergy (positive) | counted | heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)" | yes: "Functionally a non-disparagement obligation"; the summary says it "arguably qualifies" |
| 5–8 | same answer | Berkshire Hills, Digital Cinema (positives); Creditcards.com, Rand (negatives) | counted | the same Borderline heading | by the rule, yes (hedges count); by plain meaning, doubtful: offered as related provisions, and the summary calls the four "mark-usage restrictions or termination triggers rather than general non-disparagement covenants". 0.43 → 0.63 for the question (P 0.60, R 0.67) |
| 9 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-19 | Prudential (negative) | counted | heading "Borderline / special case" | **no**: "an insurance arrangement itself rather than a covenant to carry liability coverage, so I list it separately"; the answer's count, 22, leaves it out. 1.00 → 0.98 |
| 10 | bench-20261014-2328-cuad-fable, fable, cuad-key-18 | Coherus (negative) | counted | heading "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)" | by the rule, yes; by plain meaning, a hedge: "not labeled as, and weaker than, a formal ROFN" |
| 11 | same answer | Revolution Medicines (positive) | counted | the same heading | by the rule, yes; by plain meaning, **no**: "a priority between the parties to deal with third parties, not a ROFR/ROFN". The key labels it positive |
| 12 | same answer | El Pollo Loco (negative) | counted | the same heading | by the rule, yes; by plain meaning, **no**: "the Development Agreement doesn't itself set out the ROFR". Score unchanged, 0.667 |
| 13 | bench-20261015-0011-cuad-astra, gpt-6-astra, cuad-key-16 | eDiets (negative) | counted | lead-in "**One additional keyword match, not counted above:**": the reject phrase comes after the leading clause | **no**: the answer says it is not counted. 0.667 → 0.632 |

All 3 drops are of contracts the key does not label, each rejected by the answer's own heading. Of the 10 additions,
4 are positives. Items 9, 11, 12 and 13 count a contract the answer says is not a match, and 5–8 and 10 count ones
it offers as weaker or related. Items 9–12 are the cost of reading every hedge as offered, and item 13 the cost of
reading only a label's opening and its leading clause.

## The three cases #816 names

- **synthetic-key-20** (bench-20261015-0032-synthetic-fable): Skylight and Northstar are not counted, 1.00, as on
  main. "**Context on what was excluded:**" rejects, its leading clause ending with "excluded". The issue's 0.50
  predates #814. Without that lead-in both would count, since the prose after it is not read (pinned).
- **synthetic-key-8** (same run): 0.96, as on main. The extra contract is 0050, not 0048: "0048 is a July 2025
  invoice — excluded" names 0048 by no form the key holds. 0050 is named by its invoice number under the bulleted
  label "- **Excluded near-misses:**". A bulleted label is not a lead-in and line labels are not read, so 0050 is
  counted (an accepted false keep, pinned).
- **cuad-key-16** (bench-20261015-2259-cuad-fable-rerun): Berkshire Hills, Cybergy and Digital Cinema are counted:
  0.43 → 0.63 (P 0.60, R 0.67). eDiets, under "Not a non-disparagement clause", is counted too: denials are not read.
  The first run (bench-20261014-2328-cuad-fable) scores 0.67 under both rules.

## What this does not show

- That section labels capture what answers reject. They capture what an answer marks with an explicit reject phrase
  at a heading's or lead-in's start or at the end of its leading clause; anything said in a bullet, in prose, as a
  denial, or later in a label is counted. On these runs that is precision lost by the narrating cloud subjects,
  which the reports already read as a floor.
- Anything about the French phrases on real data: no stored list answer is in French with a rejecting label.
