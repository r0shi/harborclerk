# Offline rescore of every stored run under the #816 list rule, 2026-10-17

- Scorer before: `scripts/test_corpora/runner/answer_key.py` at `c2614ff` (main, with #814's exclusion-section rule)
- Scorer after: the same file on `fix/816-list-scorer-negation`
- Machine: ix. No instance, no model and no cloud call: USD 0.
- Runs: every directory under the eval workdir's `results/` (20). Nothing there was written.
- Keys: the committed `questions/keyed/{cuad,synthetic}.key.json`, for both scorers, so a difference is the rule's
  alone. A run from September was scored at the time with the key as it was then, so these numbers need not match
  the ones in its own report.
- Method: the cloud-subject rows come from `report.py`'s `cloud_subject_table` and the phase-4 rows from its
  `model_table` over cells recomputed by `sweep.py`'s `key_score_cell`, each run once with the old module and once
  with the new one swapped in. Item-level differences come from `named_contracts` with the `doc_ids` map each
  scoring site builds. 154 stored answers to keyed list questions were compared; every changed item below was
  read in its answer.

## The rule

Two readings of what a list answer's own words say about an item, both in the module docstring.

**Headings.** A heading or bold lead-in rejects its section when it holds a reject phrase ("excluded", "not
counted", "not a match", "near-miss", "false positive", "rejected", "does not qualify", French "exclu", "écarté",
"non retenu" and the like) or a negation that no qualifier softens within three words. A qualifier negates a degree,
a form, a certainty or the extent of the match ("not a classic covenant", "not described as irrevocable", "not
confirmed", "not exhaustive"); a negated infinitive ("covenants not to disparage") states the clause. Every other
label keeps its section, hedges included: "Borderline / related", "Related but narrower", "with qualifications". #814
read "borderline" as a reject and had no word for "Not a non-disparagement clause" or "(NOT Customer Solicitation
Bans)". A hedge beside an unqualified negation ("Mentions / borderline (… not operative ROFRs …)") does not save its
section.

**Prose.** A sentence opening with a rejecting "Label:" is read as a heading over that sentence. Otherwise a mention
is rejected by a cue within 10 words of it in its sentence (a name counts as one word): a reject phrase, a negated
predicate ("is not", "does not", "isn't", "n'est pas"), "rather than", or, directly before the name, "not", "except",
"sauf". The guards: quotations are not read; "not only", "not limited to" and negated infinitives are not
rejections; a negated predicate in a subordinate clause or after a possessive is about something else; a contrast
word ends a cue's reach unless the cue's clause names nothing; an active reject verb rejects only its object. An
item counts when any of its mentions survives.

**The window.** The farthest rejection read in the stored answers is nine words from its name. Over the 154
answers the window was swept from 3 to 20: seven words reads 6 of the 10 rejected mentions, nine and above read all
10, and twenty reads nothing more. Ten is a clause with a word of slack; on this data the guards, not the window,
keep a cue about something else off a name.

## Before and after

Cloud subjects (phase-1 baselines), from `cloud_subject_table`:

| run | model | corpus | answer key, before → after | lists, before → after |
|---|---|---|---|---|
| rubric-20260921-1754 | claude-sonnet-4-6 | cuad | 0.92 → 0.92 | 0.72 → 0.74 |
| bench-20260924-0724-keyed | claude-sonnet-4-6 | cuad | 0.91 → 0.92 | 0.72 → 0.74 |
| bench-20260924-2233-keyed-gemma | claude-sonnet-4-6 | cuad | 0.91 → 0.92 | 0.72 → 0.74 |
| bench-20260928-1050-keyed | claude-sonnet-4-6 | cuad | 0.93 → 0.93 | 0.76 → 0.77 |
| bench-20261014-2328-cuad-fable | claude-fable-5-1 | cuad | 0.95 → 0.95 | 0.82 → 0.82 |
| bench-20261015-0011-cuad-astra | gpt-6-astra | cuad | 0.96 → 0.96 | 0.87 → 0.87 |
| bench-20261015-0032-synthetic-fable | claude-fable-5-1 | synthetic | 1.00 → 1.00 | 0.99 → 1.00 |
| bench-20261015-0106-synthetic-astra | gpt-6-astra | synthetic | 1.00 → 1.00 | 1.00 → 1.00 |
| bench-20261015-2259-cuad-fable-rerun | claude-fable-5-1 | cuad | 0.95 → 0.96 | 0.84 → 0.88 |

Local models (phase 4), from `model_table`: every model's answer-key mean is unchanged to two places in every keyed
run (bench-20260924-0724-keyed, bench-20260924-2233-keyed-gemma, bench-20260928-1050-keyed, rubric-20260921-1754).
Four units changed, all in bench-20260928-1050-keyed, each by dropping a contract the answer itself says does not
qualify:

| model | question | before → after | named | why |
|---|---|---|---|---|
| qwen35-4b | cuad-key-17 | 0.500 → 0.571 | 6 → 4 | items 8–9 below |
| qwen35-9b | cuad-key-18 | 0.593 → 0.667 | 11 → 8 | items 10–12 |
| qwen36-35b-a3b | cuad-key-16 | 0.000 → 0.000 | 3 → 1 | items 13–14 |
| qwen38-27b | cuad-key-15 | 0.462 → 0.500 | 5 → 4 | item 15 |

## Every item whose credit changed

26 items in 12 answers. "Right" is by the answer's plain meaning, read in full.

| # | run, model, question | item (key) | change | caused by | right? |
|---|---|---|---|---|---|
| 1–3 | bench-20260924-0724-keyed, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Contracts With Employee-Only Non-Solicitation Clauses (NOT Customer Solicitation Bans)"; its text: "apply only to **employees**, not customers" | yes |
| 4–6 | bench-20260924-2233-keyed-gemma, sonnet-4-6, cuad-key-15 | the same three | dropped | the same baseline answer, the same heading | yes |
| 7 | bench-20260928-1050-keyed, sonnet-4-6, cuad-key-17 | RaeSystems (negative) | dropped | prose: "The RaeSystemsInc Co-Branding Agreement only has *revocable* licenses, so I'll exclude it." | yes |
| 8–9 | bench-20260928-1050-keyed, qwen35-4b, cuad-key-17 | Legacy Education, Southern Star (negatives) | dropped | heading "## Not Found:", over "The search did not find any contracts containing **perpetual** license grants in [both] … as these documents contained revocable licenses" | yes |
| 10–12 | bench-20260928-1050-keyed, qwen35-9b, cuad-key-18 | Stamps.com, Paperexchange, Impresse (negatives) | dropped | prose: "(e.g., *StampscomInc*, *PaperexchangeComInc*, *ImpresseCorp*), but these are procedural dispute resolution mechanisms rather than substantive first-offer or first-refusal rights" | yes |
| 13–14 | bench-20260928-1050-keyed, qwen36-35b-a3b, cuad-key-16 | HF Enterprises, WaterNow (negatives) | dropped | prose: "(such as … *WaterNowInc Distributor Agreement*, and *HfEnterprisesInc Development Agreement*), they do not appear to contain explicit **non-disparagement** language" | yes |
| 15 | bench-20260928-1050-keyed, qwen38-27b, cuad-key-15 | Coherus (negative) | dropped | lead-in "**Contracts with Non-Solicitation Clauses Limited to Employees (Not Customers):**" | yes |
| 16 | bench-20261015-0032-synthetic-fable, fable, synthetic-key-8 | 0050 (negative) | dropped | sentence label: "- **Excluded near-misses:** invoices *due* in February but issued in January — … `0050` (GS-2025-00847, Jan 3) … — were not included" | yes |
| 17 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-16 | eDiets (negative) | dropped | heading "Not a non-disparagement clause (word appears only incidentally)" | yes |
| 18 | same answer | Cybergy (positive) | counted | heading "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)" | yes: "Functionally a non-disparagement obligation", and the summary says it "arguably qualifies" |
| 19–22 | same answer | Berkshire Hills, Digital Cinema (positives); Creditcards.com, Rand (negatives) | counted | the same Borderline heading | by the decided rule, yes; by the answer's plain meaning, doubtful: offered as related provisions, and the summary calls the four "mark-usage restrictions or termination triggers rather than general non-disparagement covenants" |
| 23 | bench-20261015-2259-cuad-fable-rerun, fable, cuad-key-19 | Prudential (negative) | counted | heading "Borderline / special case" | **no**: "an insurance arrangement itself rather than a covenant to carry liability coverage, so I list it separately". The answer's own count, 22, leaves it out. 1.00 → 0.98 |
| 24–26 | rubric-20260921-1754, sonnet-4-6, cuad-key-15 | Clickstream, Coherus, Phasebio (negatives) | dropped | heading "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)" | yes |

All 20 drops are of contracts the key does not label, and each is one the answer rejected in its own words. Of the
6 additions, 3 are positives. Prudential (23) is the one change that reads an answer against its plain meaning,
and it is the cost the decision accepts: a heading vocabulary cannot tell "listed separately, not counted" from
"weaker but offered". Items 19–22 are the same question in a softer form: the key labels two of the four positive.

Prose rejection elsewhere, with no effect on a score: three Coherus mentions (gemma4-26b-a4b and gemma4-12b
"employees rather than customers [Coherus…]"; qwen35-9b "It does not contain a prohibition against soliciting the
other party's customers [Coherus…]") are rejected in their sentences, but each answer names Coherus elsewhere
without a cue, so it still counts. Every mention the prose rule rejects in the stored answers is of a contract the
key does not label.

## The three cases #816 names

- **synthetic-key-20** (bench-20261015-0032-synthetic-fable): Skylight and Northstar are not counted, 1.00. Main
  already scores this 1.00: the issue's 0.50 predates #814, whose lead-in rule drops "**Context on what was
  excluded:**". The prose rule alone would not: "— none target Acme Corp" uses no cue it holds, and "those don't
  target Acme Corp" is sixteen words from "Harbour Lights". Pinned as a stated cost.
- **synthetic-key-8** (same run): the extra contract was 0050, not 0048. "0048 is a July 2025 invoice — excluded"
  names 0048 by no form the key holds (its aliases are its total); 0050 was named by its unique invoice number in a
  bulleted bold label, which #814's lead-in rule does not read. Dropped by the sentence-label rule: 0.96 → 1.00. A
  test pins that the quoted line, with 0048 named by its stem, is read as a rejection.
- **cuad-key-16** (bench-20261015-2259-cuad-fable-rerun): Berkshire Hills, Cybergy and Digital Cinema are counted,
  eDiets is not: 0.43 → 0.67 (P 0.67, R 0.67). The first run (bench-20261014-2328-cuad-fable) scores 0.67 under
  both rules, so the two runs now agree on this question, as they did, at 0.63 each, with the exclusion rule off.

## What this does not show

- That the rule reads rejection in general. It reads the phrasings in 154 answers from three cloud subjects and eight
  local models, and the tests pin each guard against a made-up sentence. A new model's way of writing a rejection, or of
  negating something else near a name, may fall outside both.
- Anything about the French prose cues on real data: no stored list answer is in French with a rejection in it.
