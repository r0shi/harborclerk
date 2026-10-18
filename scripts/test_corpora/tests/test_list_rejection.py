"""#816: what a list answer's own words say about an item it names. A heading that rejects takes its section out; a
heading that hedges does not; a name the prose rejects where it stands does not count there. The three answers the
issue names are fixtures, copied from the stored runs, so that these tests do not depend on a results directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.test_corpora.runner.answer_key import PROSE_WINDOW, named_contracts, rejecting_label, score

FIXTURES = Path(__file__).parent / "fixtures" / "list_rejection"
HARBOUR, SKYLIGHT, NORTHSTAR = "Harbour Lights", "Skylight", "Northstar"
CAMPAIGNS = [HARBOUR, NORTHSTAR, SKYLIGHT]  # synthetic-key-20's universe
CAMPAIGN_ALIASES = {HARBOUR: ["Harbor Lights"]}


def named(text: str) -> set[str]:
    return named_contracts(text, CAMPAIGNS, CAMPAIGN_ALIASES)


# ── headings ──


@pytest.mark.parametrize(
    "label",
    [
        # A reject phrase, whatever else the label says (each observed in a stored answer unless marked).
        "False positive (excluded)",
        "Reviewed and excluded (matched the keywords but are not license grants)",
        "Excluded — near-misses worth noting",
        "Near-misses (reviewed and excluded)",
        "Near-miss worth flagging (not Delaware-governed)",
        "One additional keyword match, not counted above:",
        "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)",
        "Rejected",  # not observed
        "Non-match",  # not observed
        "Contrats écartés",  # not observed, French
        "Non retenus",  # not observed, French
        "Faux positifs",  # not observed, French
        # A negation that no qualifier softens: it denies the clause outright.
        "Not a non-disparagement clause (word appears only incidentally)",
        "⚠️ Contracts With Employee-Only Non-Solicitation Clauses (NOT Customer Solicitation Bans)",
        "Contracts with Non-Solicitation Clauses Limited to Employees (Not Customers):",
        "Not Found:",
        "Ce n'est pas une clause de non-dénigrement",  # not observed, French
        # A hedge does not save a section whose label also denies outright, nor one whose hedge is negated.
        "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)",
        "Not related",  # not observed
    ],
)
def test_a_heading_that_rejects_membership_rejects_its_section(label):
    assert rejecting_label(label)


@pytest.mark.parametrize(
    "label",
    [
        # Hedges: the answer still offers the items, as weaker or partial matches.
        "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)",
        "Borderline / special case",
        "Related but narrower: trademark / licensed-material use restrictions (3)",
        "Related provisions worth including, with qualifications",
        "🟡 Contracts with Anti-Disparagement Language (Functional Equivalents)",
        "Partial matches",  # not observed
        "Weaker matches",  # not observed
        "Arguable cases",  # not observed
        "Possible matches (not confirmed)",  # not observed: a negated certainty is a hedge
        "Clauses connexes",  # not observed, French
        # A negation of a degree, a form or the extent of the match, not of the match.
        "Perpetual only (not described as irrevocable)",
        "Matches (not exhaustive)",  # not observed
        # A negated infinitive states the clause.
        "Contracts with a genuine non-disparagement clause (a party covenants not to disparage the other)",
        # Neither a reject phrase nor a negation ("no" is not one, and "exclusive" is not "excluded").
        "Disparagement as a termination trigger only — no affirmative covenant (1)",
        "Matches (no duplicates)",
        "Exclusive license grants (2)",
        "Contracts governed by Delaware law — 8 found",
    ],
)
def test_a_heading_that_hedges_or_qualifies_keeps_its_section(label):
    assert not rejecting_label(label)


def test_a_hedged_section_counts_and_a_rejecting_one_does_not():
    text = (
        "## Campaigns for Acme Corp\n\n- Harbour Lights\n\n"
        "## Borderline / related (mention Acme but not a classic Acme brief)\n\n- Skylight\n\n"
        "## Not an Acme campaign\n\n- Northstar\n"
    )
    assert named(text) == {HARBOUR, SKYLIGHT}
    # The same answer under the rule #814 left (borderline was a reject word) named Harbour Lights and Northstar.


# ── prose: each cue ──


@pytest.mark.parametrize(
    "rejection",
    [
        "Skylight — excluded.",
        "Skylight was excluded after reading.",
        "I excluded Skylight.",
        "Skylight only targets Northwind, so I'll exclude it.",
        "Skylight is not a match.",
        "Skylight does not target Acme Corp.",
        "Skylight doesn't target Acme Corp.",
        "Skylight is not an Acme campaign.",
        "Skylight isn't an Acme campaign.",
        "Skylight was not counted.",
        "Skylight was rejected.",
        "Skylight does not qualify.",
        "The campaign is Harbour Lights rather than Skylight.",
        "Harbour Lights, not Skylight.",
        "Every campaign except Skylight.",
        "Skylight n'est pas une campagne d'Acme.",
        "Skylight ne cible pas Acme Corp.",
        "Toutes les campagnes sauf Skylight.",
        "Skylight est exclu.",
        "Excluded: Skylight.",
        "- **Not counted:** Skylight.",
    ],
)
def test_a_name_the_prose_rejects_where_it_stands_is_not_named(rejection):
    answer = f"Harbour Lights targets Acme Corp.\n{rejection}"
    assert named(answer) == {HARBOUR}


@pytest.mark.parametrize(
    "affirmation",
    [
        "Skylight targets Acme Corp.",
        "Skylight is an Acme campaign.",
        "Skylight was included.",
        "Skylight also matches.",
    ],
)
def test_without_a_cue_a_name_in_prose_is_named(affirmation):
    assert named(affirmation) == {SKYLIGHT}


# ── prose: the false negations it must not read ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "not only X but also Y" affirms both.
        ("Not only Harbour Lights but also Skylight targets Acme Corp.", {HARBOUR, SKYLIGHT}),
        ("Harbour Lights is not only the largest campaign but also the oldest.", {HARBOUR}),
        ("Harbour Lights is not limited to Acme Corp.", {HARBOUR}),
        # A negated infinitive is an obligation, not a verdict.
        ("Acme agreed not to cite Skylight in its filings.", {SKYLIGHT}),
        # A quotation is the document's words: "is not for retail buyers" is about the audience.
        ('Harbour Lights: the brief says "this campaign is not for retail buyers".', {HARBOUR}),
        # A negation inside a subordinate clause is about that clause's subject.
        ("Harbour Lights has a budget that is not yet approved.", {HARBOUR}),
        ("Harbour Lights targets Acme Corp, which does not run its own ads.", {HARBOUR}),
        # A possessive: the term, not the item, is negated.
        ("Northstar's budget is not final.", {NORTHSTAR}),
        ("Harbour Lights' launch date is not set.", {HARBOUR}),
        # A contrast word ends the negation's reach when the other side names its own item...
        ("Skylight is not one, but Harbour Lights is.", {HARBOUR}),
        ("Harbour Lights targets Acme, whereas Skylight does not.", {HARBOUR}),
        # ...and "rather than" rejects what follows it, not the name before it.
        ("Harbour Lights targets Acme rather than Northwind.", {HARBOUR}),
        # An active reject verb rejects its object, which here is not a name.
        ("After excluding duplicates, the campaigns are Harbour Lights and Skylight.", {HARBOUR, SKYLIGHT}),
        ("The Skylight brief excludes the retail segment.", {SKYLIGHT}),
        # "except for <a purpose>" is a carve-out in the clause, and the name cited after it is not its object.
        ("The agency shall not reuse assets, except for joint marketing purposes [Skylight, p. 3].", {SKYLIGHT}),
        # A reject word with a name right after it rejects that name, not the one before it.
        ("I counted Harbour Lights and excluded Skylight.", {HARBOUR}),
        ("Skylight is excluded, Harbour Lights included.", {HARBOUR}),
        # "exclusive" is not "excluded".
        ("Harbour Lights was crafted exclusively for Acme Corp.", {HARBOUR}),
    ],
)
def test_a_cue_that_negates_something_other_than_the_item_does_not_reject_it(text, expected):
    assert named(text) == expected


def test_a_cue_whose_own_clause_names_nothing_reaches_back_to_the_names_before_it():
    """qwen35-9b, bench-20260928-1050-keyed cuad-key-18: "(e.g., StampscomInc, PaperexchangeComInc, ImpresseCorp),
    but these are procedural dispute resolution mechanisms rather than substantive first-offer ... rights"."""
    assert named("Skylight and Northstar have briefs too, but these are not for Acme Corp.") == set()
    assert named("Skylight and Northstar have briefs, but these are retail campaigns rather than Acme ones.") == set()


def test_the_window_is_ten_words_with_a_name_counting_as_one():
    filler = " ".join(["word"] * PROSE_WINDOW)
    assert PROSE_WINDOW == 10
    assert named(f"Skylight {filler} is not an Acme campaign.") == set(), "ten words between: rejected"
    assert named(f"Skylight {filler} more is not an Acme campaign.") == {SKYLIGHT}, "eleven: out of reach"
    # A multi-word name between the cue and the item is one word, so a list of names does not run out the window.
    seven = " ".join(["word"] * 7)  # "find", seven words, a two-word name, "or": ten
    assert named(f"The search did not find {seven} Harbor Lights or Skylight.") == set()


def test_a_sentence_ends_the_reach_and_a_name_listed_anywhere_else_still_counts():
    assert named("Skylight is not an Acme campaign. Northstar is.") == {NORTHSTAR}
    assert named("Skylight is not an Acme campaign; Northstar is.") == {NORTHSTAR}
    # Decided: a sentence that rejects a name does not cancel another that lists it.
    assert named("Skylight is not an Acme campaign.\n\n| Campaign |\n|---|\n| Skylight |\n") == {SKYLIGHT}
    # A table cell is a sentence: a cue in the next cell does not reach the name.
    assert named("| Skylight | does not target retail |\n") == {SKYLIGHT}


# ── the three answers #816 names ──


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_fable_synthetic_key_20_counts_only_harbour_lights():
    """bench-20261015-0032-synthetic-fable: "Answer: Only one campaign — Harbour Lights", then Skylight and
    Northstar under "**Context on what was excluded:**". The lead-in (#814's rule, kept) is what takes them out."""
    answer = _fixture("fable-synthetic-key-20.md")
    entry = {"kind": "list", "positives": [HARBOUR], "universe": CAMPAIGNS, "aliases": CAMPAIGN_ALIASES}
    assert named(answer) == {HARBOUR} and score(entry, answer)["score"] == 1.0
    # The cost, stated: the prose that follows the lead-in ("— none target Acme Corp", "those don't target Acme
    # Corp" sixteen words on) is out of the prose rule's reach, so without the lead-in both are counted.
    unmarked = answer.replace("**Context on what was excluded:**", "Context:")
    assert named(unmarked) == set(CAMPAIGNS)


KEY8_POSITIVES = [f"{n:04d}_invoice" for n in (3, 8, 10, 12, 13, 19, 29, 38, 40, 44, 46, 57)]
KEY8_ALIASES = {  # the committed synthetic key's aliases for these invoices (questions/keyed/synthetic.key.json)
    "0003_invoice": ["10453.69", "10453"],
    "0008_invoice": ["21356.00", "21356"],
    "0010_invoice": ["10734.07", "10734"],
    "0012_invoice": ["17195.51", "17195"],
    "0013_invoice": ["19712.33", "19712"],
    "0019_invoice": ["7263.84", "7263"],
    "0026_invoice": ["10075.85", "10075"],
    "0029_invoice": ["13772.63", "13772"],
    "0031_invoice": ["17666.40", "17666"],
    "0038_invoice": ["9521.30", "9521"],
    "0040_invoice": ["7291.01", "7291"],
    "0044_invoice": ["19062.83", "19062"],
    "0046_invoice": ["CYB-2025-00847", "18797.67", "18797"],
    "0048_invoice": ["14060.00", "14060"],
    "0050_invoice": ["GS-2025-00847", "12720.41", "12720"],
    "0052_invoice": ["14230.00", "14230"],
    "0055_invoice": ["11647.00", "11647"],
    "0056_invoice": ["7717.14", "7717"],
    "0057_invoice": ["16210.80", "16210"],
}


def test_fable_synthetic_key_8_does_not_count_the_invoices_it_excludes():
    """bench-20261015-0032-synthetic-fable: twelve February invoices, then "- **Excluded near-misses:** ... `0050`
    (GS-2025-00847, Jan 3) ... were not included". 0050 is named by its unique invoice number; a bulleted bold
    label is not a lead-in, so #814's rule kept it and the answer scored 0.96. ("0048 is a July 2025 invoice —
    excluded", the line the issue quotes, names 0048 by no form the key holds.)"""
    answer = _fixture("fable-synthetic-key-8.md")
    universe = sorted(KEY8_ALIASES)
    entry = {"kind": "list", "positives": KEY8_POSITIVES, "universe": universe, "aliases": KEY8_ALIASES}
    assert named_contracts(answer, universe, KEY8_ALIASES) == set(KEY8_POSITIVES)
    assert score(entry, answer)["score"] == 1.0
    # Named by its stem, the line the issue quotes is read as it was meant; affirmed, it is named.
    rejected = answer.replace("0048 is a July", "0048_invoice is a July")
    assert "0048_invoice" not in named_contracts(rejected, universe, KEY8_ALIASES)
    affirmed = "0048_invoice is a February 2025 invoice. My enumeration is complete."
    assert named_contracts(affirmed, universe, KEY8_ALIASES) == {"0048_invoice"}


KEY16_DOC_IDS = {  # the ids the answer wrote out, as the baseline's citations map them
    "01344d95-68ef-4660-9286-a6308349bd1a": "RandWorldwideInc_20010402_8-KA_EX-10.2_2102464_EX-10.2_Co-Branding Agreement",
    "3c40cba1-a4cc-47f4-a1a6-2011dc83888a": "EdietsComInc_20001030_10QSB_EX-10.4_2606646_EX-10.4_Co-Branding Agreement",
    "43095b35-9378-4710-8e52-06ae5f8f292a": (
        "BerkshireHillsBancorpInc_20120809_10-Q_EX-10.16_7708169_EX-10.16_Endorsement Agreement"
    ),
    "4be07132-7e4f-4b89-8ff6-4e48d0884e88": (
        "DigitalCinemaDestinationsCorp_20111220_S-1_EX-10.10_7346719_EX-10.10_Affiliate Agreement"
    ),
    "5db63bc4-f1f1-4073-8b82-332ae253c31c": "PelicanDeliversInc_20200211_S-1_EX-10.3_11975895_EX-10.3_Development Agreement2",
    "7de22e9f-30e5-4d0f-b1a9-5345b4c39de0": (
        "StaarSurgicalCompany_20180801_10-Q_EX-10.37_11289449_EX-10.37_Distributor Agreement"
    ),
    "a30ad65d-2370-4336-87f9-0f8823e8e096": (
        "PapaJohnsInternationalInc_20190617_8-K_EX-10.1_11707365_EX-10.1_Endorsement Agreement"
    ),
    "b7f37b40-c3d8-4630-b91e-9d3c862202c6": "CybergyHoldingsInc_20140520_10-Q_EX-10.27_8605784_EX-10.27_Affiliate Agreement",
    "d5cc977c-30a6-4b59-964b-c1f215a382ec": "BizzingoInc_20120322_8-K_EX-10.17_7504499_EX-10.17_Endorsement Agreement",
    "e63b7241-fb85-4319-9691-94ffa08a7054": "CreditcardscomInc_20070810_S-1_EX-10.33_362297_EX-10.33_Affiliate Agreement",
}
KEY16_POSITIVES = [
    KEY16_DOC_IDS["43095b35-9378-4710-8e52-06ae5f8f292a"],  # Berkshire Hills
    KEY16_DOC_IDS["b7f37b40-c3d8-4630-b91e-9d3c862202c6"],  # Cybergy
    KEY16_DOC_IDS["4be07132-7e4f-4b89-8ff6-4e48d0884e88"],  # Digital Cinema
    KEY16_DOC_IDS["a30ad65d-2370-4336-87f9-0f8823e8e096"],  # Papa John's
    KEY16_DOC_IDS["5db63bc4-f1f1-4073-8b82-332ae253c31c"],  # Pelican Delivers
    KEY16_DOC_IDS["7de22e9f-30e5-4d0f-b1a9-5345b4c39de0"],  # STAAR
    "InnerscopeHearingTechnologiesInc_20181109_8-K_EX-10.6_11419704_EX-10.6_Distributor Agreement",
    "PerformanceSportsBrandsInc_20110909_S-1_EX-10.10_7220214_EX-10.10_Endorsement Agreement",
    "SmartRxSystemsInc_20180914_1-A_EX1A-6 MAT CTRCT_11351705_EX1A-6 MAT CTRCT_Distributor Agreement",
]


def test_fable_rerun_cuad_key_16_counts_its_borderline_section_and_not_its_rejected_one():
    """bench-20261015-2259-cuad-fable-rerun: three headings. "Borderline / related provisions (contain disparagement
    language but not a classic non-disparagement covenant)" holds Berkshire Hills, Cybergy and Digital Cinema, CUAD
    positives, which #814's rule dropped ("borderline" and "not a"): 0.43. "Not a non-disparagement clause" holds
    eDiets, which #814's rule kept."""
    answer = _fixture("fable-rerun-cuad-key-16.md")
    universe = sorted(set(KEY16_DOC_IDS.values()) | set(KEY16_POSITIVES))
    got = named_contracts(answer, universe, None, KEY16_DOC_IDS)
    berkshire, cybergy, cinema = KEY16_POSITIVES[:3]
    assert {berkshire, cybergy, cinema} <= got
    assert KEY16_DOC_IDS["3c40cba1-a4cc-47f4-a1a6-2011dc83888a"] not in got, "eDiets is under the rejecting heading"
    result = score({"kind": "list", "positives": KEY16_POSITIVES, "universe": universe}, answer, KEY16_DOC_IDS)
    assert (result["named"], result["right"]) == (9, 6) and result["score"] == pytest.approx(2 / 3)
