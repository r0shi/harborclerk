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

## The rule

Only labels are read for rejection: headings, bold lead-ins at the margin, and the bold or colon-terminated label
that opens a line or bullet. A heading or lead-in that rejects takes its section out, as under #814. A line label
that rejects and names no item takes out what follows it on its line, up to the next semicolon or sentence end
("Not counted: eDiets; counted: Staar" keeps Staar); a full stop after an abbreviation or an initial ("Inc.",
"Jan.", "U.S.") does not end the sentence, and an inner colon ("doc_id:", "Skylight: targets Northwind") does not
end the reach. A plain label counts only if it is a single clause, with no comma, semicolon or dash before its colon.
A line that is only a rejecting label ("Excluded:", "- **Excluded near-misses:**") takes the list items under it, up
to a blank line, a heading, another label line, a line that is not a list item, or an item indented less than the
label (at its indentation, for a bulleted label), so an indented "Exclusions:" under one contract's bullet takes
only its own sub-list. Line labels of more than twelve words are sentences, not labels.

A label rejects when:

- it **opens** with a reject phrase: excluded, exclusions, not counted, not included, not a match, non-match,
  near-miss, false positive, rejected, ruled out, does not qualify, and the French equivalents; or
- its **leading clause**, the words before the first parenthesis, comma, colon, semicolon or dash, **ends** with a
  reject phrase after nothing but words for the items and framing words: "Reviewed and excluded", "Context on what
  was excluded", "Notable Contracts That Were Close But Do NOT Qualify". Any other word means the phrase describes
  the clause: "Punitive damages excluded", "Employee solicitation not included", "Sale once the first offer is
  rejected".

It does not reject when:
- after a contrast, it says the item qualifies with no negation ("Not included above but also qualifies"); "Close
  But Do NOT Qualify" still rejects;
- what follows the label, after its own colon or on the next line, is only "none", "nothing", "n/a", "zero" or the
  like, alone or followed by punctuation or a conjunction ("## Exclusions: none", "Exclusions: none; Staar and Papa
  John's both qualify"). "None of these contains a genuine covenant:", "No affirmative covenant in either:" and
  "Zero-rated:" do not stop it;
- an opening participle describes something else: "Excluded from the cap", "Exclusions from the liability cap",
  "Rejected offers go to third parties", "Rejected-offer matching rights" (but "Excluded from the (final) list /
  consideration / scope / review / tally" and "Excluded due to scope" reject).

A denial of a category ("Not a non-disparagement clause") is not read, and nor is any other reject word later in a
label. Hedges count ("Borderline / related", "Mentions / borderline"), and prose is not read for rejection.

Why so little: a false drop silently lowers recall on a right answer, while a false keep lowers only precision,
which the reports read as a floor for narrating models. Rules that read negation anywhere in a heading, verdicts in
prose, or a denial of the question's category drop true matches on plausible answers. The clauses asked for are
themselves prohibitions ("Irrevocable licenses (may not be revoked)"). A clause's description uses a verdict's words
("if the offer is rejected, STAAR may sell"). And a denial names a subtype as readily as the category ("Not a New
York law contract (Delaware governs …)", "Not a post-termination non-disparagement clause"). Across the 154 stored
answers, a prose rule would have made two drops and a denial rule one.

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
| bench-20261015-2259-cuad-fable-rerun | claude-fable-5-1 | cuad | 0.95 → 0.96 | 0.84 → 0.87 |

Local models (phase 4), from `model_table`, in bench-20260924-0724-keyed, bench-20260924-2233-keyed-gemma,
bench-20260928-1050-keyed and rubric-20260921-1754: **no unit changed.**

## Every item whose credit changed

14 items in 6 answers. "Right" is by the answer's plain meaning, read in full.

| # | run, model, question | item (key) | change | caused by | right? |
|---|---|---|---|---|---|
| 1 | bench-20261015-0032-synthetic-fable, fable, synthetic-key-8 | 0050 (negative) | dropped | line label "- **Excluded near-misses:** invoices *due* in February but issued in January — … `0050` (GS-2025-00847, Jan 3) … — were not included" | yes. 0.96 → 1.00 |
| 2–4 | rubric-20260921-1754, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)": the leading clause ends "Do NOT Qualify" after framing words only | yes. 0.40 → 0.50 |
| 5 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | Cybergy (positive) | counted | heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)" | yes: "Functionally a non-disparagement obligation"; the summary says it "arguably qualifies" |
| 6–9 | same answer | Berkshire Hills, Digital Cinema (positives); Creditcards.com, Rand (negatives) | counted | the same Borderline heading | by the rule, yes (hedges count); by plain meaning, doubtful: offered as related provisions, and the summary calls the four "mark-usage restrictions or termination triggers rather than general non-disparagement covenants". 0.43 → 0.63 for the question (P 0.60, R 0.67) |
| 10 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-19 | Prudential (negative) | counted | heading "Borderline / special case" | **no**: "an insurance arrangement itself rather than a covenant to carry liability coverage, so I list it separately"; the answer's count, 22, leaves it out. 1.00 → 0.98 |
| 11 | bench-20261014-2328-cuad-fable, fable, cuad-key-18 | Coherus (negative) | counted | heading "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)" | by the rule, yes; by plain meaning, a hedge: "not labeled as, and weaker than, a formal ROFN" |
| 12 | same answer | Revolution Medicines (positive) | counted | the same heading | by the rule, yes; by plain meaning, **no**: "a priority between the parties to deal with third parties, not a ROFR/ROFN". The key labels it positive |
| 13 | same answer | El Pollo Loco (negative) | counted | the same heading | by the rule, yes; by plain meaning, **no**: "the Development Agreement doesn't itself set out the ROFR". Score unchanged, 0.667 |
| 14 | bench-20261015-0011-cuad-astra, gpt-6-astra, cuad-key-16 | eDiets (negative) | counted | lead-in "**One additional keyword match, not counted above:**": the reject phrase comes after the leading clause | **no**: the answer says it is not counted. 0.667 → 0.632 |

All 4 drops are of contracts the key does not label, each rejected by the answer's own label. Of the 10 additions,
4 are positives. Items 10, 12, 13 and 14 count a contract the answer says is not a match, and 6–9 and 11 count ones
it offers as weaker or related. Items 10–13 are the cost of reading every hedge as offered, and item 14 the cost of
reading only a label's opening and its leading clause.

Rejections made other ways stay counted, as on main. In the stored answers that includes:
- the rerun's eDiets under "Not a non-disparagement clause (word appears only incidentally)", a denial of the
  category (cuad-key-16: 0.63, where reading the denial would give 0.67);
- RaeSystems (Sonnet 4.6, bench-20260928-1050-keyed, cuad-key-17: "…only has *revocable* licenses, so I'll exclude
  it."), said in prose;
- the contracts local models rejected with a negation ("(NOT Customer Solicitation Bans)", "Not Found:", "they do
  not appear to contain …").

All of these are unlabelled by the key.

## The three cases #816 names

- **synthetic-key-20** (bench-20261015-0032-synthetic-fable): Skylight and Northstar are not counted, 1.00, as on
  main. "**Context on what was excluded:**" rejects, its leading clause ending with "excluded". The issue's 0.50
  predates #814. Without that lead-in both would count, since the prose after it is not read (pinned).
- **synthetic-key-8** (same run): the extra contract was 0050, not 0048. "0048 is a July 2025 invoice — excluded"
  names 0048 by no form the key holds. 0050 was named by its invoice number under a bulleted bold label, which #814
  did not read and the line-label rule does: 0.96 → 1.00. Had 0048 been named by its stem in that prose line, it
  would be counted (pinned as a cost).
- **cuad-key-16** (bench-20261015-2259-cuad-fable-rerun): Berkshire Hills, Cybergy and Digital Cinema are counted:
  0.43 → 0.63 (P 0.60, R 0.67). eDiets is counted too, its heading being a denial of the category. The first run
  (bench-20261014-2328-cuad-fable) scores 0.67 under both rules. The same answer with its table written as bullets
  names the same ten contracts.

## What this does not show

- That labels capture what answers reject. They capture what an answer marks with an explicit reject phrase at a
  label's start or at the end of its leading clause; anything said in prose, as a denial, or later in a label is
  counted. On these runs that is precision lost by the narrating cloud subjects, which the reports already read as a
  floor.
- Anything about the French phrases on real data: no stored list answer is in French with a rejecting label.
