"""#816: what a list answer's own words say about an item it names. A heading with an explicit reject phrase takes its
section out; any other heading, a hedge or a prohibition, does not; a name the prose rejects with a verdict attached
to it does not count there. A false drop silently lowers recall on a right answer and a false keep only lowers
precision, so where the two pull apart these pin the keep. The three answers the issue names are fixtures, copied
from the stored runs, and so are the probes of the review of the first version of this rule (each marked "review")."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.test_corpora.runner.answer_key import named_contracts, rejecting_label, score

FIXTURES = Path(__file__).parent / "fixtures" / "list_rejection"
HARBOUR, SKYLIGHT, NORTHSTAR = "Harbour Lights", "Skylight", "Northstar"
CAMPAIGNS = [HARBOUR, NORTHSTAR, SKYLIGHT]  # synthetic-key-20's universe
CAMPAIGN_ALIASES = {HARBOUR: ["Harbor Lights"]}

STAAR = "StaarSurgicalCompany_20180801_10-Q_EX-10.37_11289449_EX-10.37_Distributor Agreement"
EDIETS = "EdietsComInc_20001030_10QSB_EX-10.4_2606646_EX-10.4_Co-Branding Agreement"
PAPA = "PapaJohnsInternationalInc_20190617_8-K_EX-10.1_11707365_EX-10.1_Endorsement Agreement"
SID, EID, PID = (
    "7de22e9f-30e5-4d0f-b1a9-5345b4c39de0",
    "3c40cba1-a4cc-47f4-a1a6-2011dc83888a",
    "a30ad65d-2370-4336-87f9-0f8823e8e096",
)
CONTRACTS, IDS = [STAAR, EDIETS, PAPA], {SID: STAAR, EID: EDIETS, PID: PAPA}
INVOICES = ["0012_invoice", "0048_invoice", "0050_invoice"]
INVOICE_ALIASES = {
    "0012_invoice": ["17195.51", "17195"],
    "0048_invoice": ["14060.00", "14060"],
    "0050_invoice": ["GS-2025-00847", "12720.41", "12720"],
}


def named(text: str) -> set[str]:
    return named_contracts(text, CAMPAIGNS, CAMPAIGN_ALIASES)


def contracts(text: str) -> set[str]:
    return named_contracts(text, CONTRACTS, None, IDS)


# ── headings ──


@pytest.mark.parametrize(
    "label",
    [
        # An explicit reject phrase (each observed in a stored answer unless marked).
        "False positive (excluded)",
        "Reviewed and excluded (matched the keywords but are not license grants)",
        "Contracts Excluded (Delaware mentioned but NOT the governing law)",
        "Excluded — near-misses worth noting",
        "Near-miss worth flagging (not Delaware-governed)",
        "One additional keyword match, not counted above:",
        "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)",
        "Mention insurance but contain **no** maintenance requirement (excluded after reading)",
        "Rejected",  # not observed
        "Non-match",  # not observed
        "Ruled out",  # not observed
        "Contrats écartés",  # not observed, French
        "Non retenus",  # not observed, French
        "Faux positifs",  # not observed, French
        # A heading that opens by denying the category.
        "Not a non-disparagement clause (word appears only incidentally)",
        "Not an Acme campaign",  # not observed
        "Ce n'est pas une clause de non-dénigrement",  # not observed, French
    ],
)
def test_a_heading_with_an_explicit_reject_phrase_rejects_its_section(label):
    assert rejecting_label(label)


@pytest.mark.parametrize(
    "label",
    [
        # Hedges: the answer still offers the items, as weaker or partial matches.
        "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)",
        "Borderline / special case",
        "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)",
        "Related but narrower: trademark / licensed-material use restrictions (3)",
        "🟡 Contracts with Anti-Disparagement Language (Functional Equivalents)",
        "Possible matches (not confirmed)",  # not observed
        # A negation that is not a reject phrase: the clauses asked for are prohibitions, and a heading describes them.
        "Irrevocable licenses (may not be revoked)",  # review
        "Licenses that can't be revoked",  # review
        "Perpetual licenses (do not expire)",  # review
        "Contracts in which a party shall not disparage the other",  # review
        "Contracts where a party may not solicit the other's customers",  # review
        "Clauses prohibiting solicitation (not limited to employees)",  # review
        "Mutual clauses (neither party may disparage)",  # review
        "Delaware law (not New York)",  # review
        "Insurance required (amount not specified)",  # review
        "One-way, not mutual",  # review
        "Not mutual",  # review
        "Perpetual only (not described as irrevocable)",
        "Contracts with a genuine non-disparagement clause (a party covenants not to disparage the other)",
        # A denial of the category that denies a degree or a form, not the match.
        "Not a classic covenant, but related",
        # A negation that the lost first version read as a rejection, and that is not an explicit one.
        "⚠️ Contracts With Employee-Only Non-Solicitation Clauses (NOT Customer Solicitation Bans)",
        "Contracts with Non-Solicitation Clauses Limited to Employees (Not Customers):",
        "Not Found:",
        # A reject phrase a negation cancels.
        "Nothing excluded",  # review
        "Not excluded",  # review
        "None rejected",  # review
        "No false positives",  # review
        "Not a near-miss",  # review
        # Not a reject phrase at all.
        "Excluding duplicates",  # review: an active verb, with its object
        "Clear matches",  # review
        "Disparagement as a termination trigger only — no affirmative covenant (1)",
        "Matches (no duplicates)",
        "Exclusive license grants (2)",
        "Contracts governed by Delaware law — 8 found",
    ],
)
def test_any_other_heading_keeps_its_section(label):
    assert not rejecting_label(label)


def test_a_hedged_or_prohibiting_section_counts_and_a_rejecting_one_does_not():
    text = (
        "## Campaigns for Acme Corp\n\n- Harbour Lights\n\n"
        "## Borderline / related (mention Acme but not a classic Acme brief)\n\n- Skylight\n\n"
        "## Not an Acme campaign\n\n- Northstar\n"
    )
    assert named(text) == {HARBOUR, SKYLIGHT}
    # review: headings that describe a prohibition keep their items
    prohibition = f"I found three.\n\n## Customer non-solicitation\n\n- `{SID}`\n\n## Irrevocable licenses (may not be revoked)\n\n- `{EID}`\n- `{PID}`\n"
    assert contracts(prohibition) == {STAAR, EDIETS, PAPA}
    one_way = f"I found three.\n\n## Mutual\n\n- `{SID}`\n\n## One-way (not mutual)\n\n- `{EID}`\n- `{PID}`\n"
    assert contracts(one_way) == {STAAR, EDIETS, PAPA}


# ── prose: verdicts attached to a name ──


@pytest.mark.parametrize(
    "rejection",
    [
        "Skylight — excluded.",
        "Skylight - excluded: it targets Northwind.",  # review
        "Skylight (excluded) targets Northwind.",
        "Skylight was excluded after reading.",
        "Skylight is excluded.",
        "I excluded Skylight.",
        "Excluded: Skylight.",
        "- **Not counted:** Skylight.",
        "Not counted: Skylight.",
        "Skylight only targets Northwind, so I'll exclude it.",
        "Skylight is not a match.",
        "Skylight isn't a match.",
        "Skylight is not one.",
        "Skylight does not qualify.",
        "Skylight doesn't qualify.",
        "Skylight was not counted.",
        "Skylight was rejected.",
        "Skylight is a false positive.",
        "Skylight is not an Acme campaign.",
        "Skylight isn't an Acme campaign.",
        "Skylight is not an Acme campaign: it targets Northwind.",  # review, item 1
        "Skylight is not an Acme campaign. It targets Northwind.",  # review
        "The campaign is Harbour Lights rather than Skylight.",
        "Harbour Lights, not Skylight.",
        "Every campaign except Skylight.",
        "Skylight n'est pas une campagne d'Acme.",
        "Skylight est exclu.",
        "Skylight ne correspond pas.",
        "Toutes les campagnes sauf Skylight.",
    ],
)
def test_a_verdict_attached_to_a_name_rejects_it(rejection):
    assert named(f"Harbour Lights targets Acme Corp.\n{rejection}") == {HARBOUR}


@pytest.mark.parametrize(
    "rejection",
    [  # review: the verdict, then a colon or semicolon and the reason
        f"eDiets (`{EID}`) is not a match: the word appears only in a warranty.",
        f"eDiets (`{EID}`) is not a match; the word appears only in a warranty.",
        f"`{EID}` (eDiets) does not qualify: the word appears only in a warranty.",
        f"eDiets (`{EID}`) — excluded: the word appears only in a warranty.",
        f"`{EID}` is not a match.",
        f"**Not a match:** `{EID}`",
    ],
)
def test_a_verdict_on_an_id_rejects_it_and_only_it(rejection):
    assert contracts(f"Staar (`{SID}`) has one.\n{rejection}") == {STAAR}


def test_a_verdict_takes_only_its_own_name_never_an_earlier_one():
    """review, item 2: the first version dropped every name within ten words before a cue."""
    assert contracts(f"Staar (`{SID}`) matches, eDiets (`{EID}`) is not a match.") == {STAAR}
    three = f"Staar (`{SID}`): insurance required. Papa (`{PID}`): insurance required. eDiets (`{EID}`) is not one."
    assert contracts(three) == {STAAR, PAPA}
    assert contracts(three.replace(f"eDiets (`{EID}`)", f"`{EID}`")) == {STAAR, PAPA}
    assert named("Harbour Lights is the only one; Skylight is not a match.") == {HARBOUR}
    assert named("Harbour Lights and Northstar match. Skylight is excluded.") == {HARBOUR, NORTHSTAR}
    assert named("I counted Harbour Lights and excluded Skylight.") == {HARBOUR}
    assert named("Skylight is excluded, Harbour Lights included.") == {HARBOUR}
    # "so I'll exclude it" and a sentence-final "— excluded" are read only when the sentence names one item before
    # them, and not over a colon or a contrast word.
    assert named("Harbour Lights and Skylight were read, so I'll exclude it.") == {HARBOUR, SKYLIGHT}
    assert named("Skylight targets Northwind — excluded.") == set()
    assert named("Harbour Lights and Skylight were read — excluded.") == {HARBOUR, SKYLIGHT}
    assert named("Skylight: a Northwind brief — excluded.") == {SKYLIGHT}
    assert named("Skylight targets Acme, but the Northwind brief — excluded.") == {SKYLIGHT}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # review, item 4: a description of the clause, not a verdict on the item.
        (
            f"- Staar (`{SID}`), §4.2: if the offer is rejected, STAAR may sell to a third party on the same terms.",
            {STAAR},
        ),
        (
            f"- Staar (`{SID}`), §4.2: right of first refusal on any sale; offers rejected by Distributor lapse.",
            {STAAR},
        ),
        (f"- Staar (`{SID}`): product liability insurance of $5M; punitive damages excluded.", {STAAR}),
        (
            f"- Staar (`{SID}`): Distributor must insure the Products, with consequential losses excluded from cover.",
            {STAAR},
        ),
        (f"- Staar (`{SID}`): insurance required, policy limits not listed in the excerpt.", {STAAR}),
        (f"1. Staar Surgical (`{SID}`) — Section 9 requires insurance, but the amount is not specified.", {STAAR}),
        (f"- `{SID}` (Staar): the license is perpetual, although it is not described as irrevocable.", {STAAR}),
        (
            f"- **StaarSurgicalCompany** (`{SID}`): Distributor shall maintain insurance. The policy amount is not stated.",
            {STAAR},
        ),
        (
            f"- StaarSurgicalCompany (`{SID}`) — Distributor must carry product liability insurance; coverage does not lapse.",
            {STAAR},
        ),
        (
            f"- StaarSurgicalCompany (`{SID}`) — the clause is mutual: neither party may solicit customers of the other.",
            {STAAR},
        ),
        (
            f"Staar Surgical Company (`{SID}`) is a distributor agreement that does not allow termination for convenience.",
            {STAAR},
        ),
        (f"Both `{SID}` and `{EID}` contain one.", {STAAR, EDIETS}),
        (f"| `{SID}` | Staar | §9.2 | Yes — not limited |", {STAAR}),
        (f"| Staar | `{SID}` | Insurance required (amount not specified) |", {STAAR}),
        (f"- Staar (`{SID}`): a perpetual license; termination does not revoke it.", {STAAR}),
        (f"- `{SID}` Staar: governed by Delaware law (not New York).", {STAAR}),
        (
            f"- `{PID}` Papa John's: the endorser will not disparage Papa John's; Papa John's will not disparage the endorser.",
            {PAPA},
        ),
        (f"I found it in `{SID}` (Staar). It is not an exhaustive list.", {STAAR}),
        (f"`{SID}` and `{EID}` both require insurance, but neither specifies amounts.", {STAAR, EDIETS}),
        (f"Staar (`{SID}`), not to be confused with eDiets, requires insurance.", {STAAR}),
        (f"Staar (`{SID}`) — Distributor can't solicit customers.", {STAAR}),
        (f"Staar (`{SID}`) — Distributor won't disparage the company.", {STAAR}),
        (f"Staar (`{SID}`) — Section 12 (Insurance). I am not certain the list is complete.", {STAAR}),
        (f"Staar (`{SID}`) requires insurance, which is not unusual.", {STAAR}),
        (f"Staar (`{SID}`): insurance is required — this isn't optional.", {STAAR}),
        (f"Staar (`{SID}`) is the only one I did not find ambiguous.", {STAAR}),
        ("StaarSurgicalCompany's license is perpetual, but it is not described as irrevocable.", {STAAR}),
        ("StaarSurgicalCompany grants a perpetual license; however, it is not described as irrevocable.", {STAAR}),
        (f"- `{SID}` - Distributor shall not solicit customers of `{PID}`.", {STAAR, PAPA}),
        (f"Staar (`{SID}`) is not a mutual covenant but binds the Distributor.", {STAAR}),
        (f"Staar (`{SID}`) is not a classic non-disparagement clause.", {STAAR}),
        # review: a negated verdict is no verdict.
        (f"Staar (`{SID}`) was not excluded: its §6.7 is a real covenant.", {STAAR}),
        (f"Staar (`{SID}`) is not a false positive: §6.7 is a real covenant.", {STAAR}),
        (f"I did not exclude Staar (`{SID}`).", {STAAR}),
        (f"Nothing was excluded: `{SID}`, `{PID}`.", {STAAR, PAPA}),
        (f"Staar (`{SID}`) — the clause is one-way; Papa John's is mutual.", {STAAR}),
        # a quotation is the document's words
        (f'Staar (`{SID}`): the clause reads "the Distributor is excluded from the Territory".', {STAAR}),
    ],
)
def test_a_description_of_the_clause_is_not_a_verdict_on_the_item(text, expected):
    assert contracts(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [  # review: campaigns
        ("Harbour Lights targets Acme Corp. Its brief is not public.", {HARBOUR}),
        ("Harbour Lights targets Acme Corp, but the budget is not set.", {HARBOUR}),
        ("Harbour Lights — targets Acme Corp (not exclusively).", {HARBOUR}),
        ("Harbour Lights targets Acme Corp; I did not find any other.", {HARBOUR}),
        ("The Harbour Lights brief targets Acme Corp, although its launch date isn't confirmed.", {HARBOUR}),
        ("Not only Harbour Lights but also Skylight targets Acme Corp.", {HARBOUR, SKYLIGHT}),
        ("Harbour Lights targets Acme rather than Northwind.", {HARBOUR}),
        ("After excluding duplicates, the campaigns are Harbour Lights and Skylight.", {HARBOUR, SKYLIGHT}),
        ("The Skylight brief excludes the retail segment.", {SKYLIGHT}),
        ("Harbour Lights was crafted exclusively for Acme Corp.", {HARBOUR}),
        ("Harbour Lights' launch date is not set.", {HARBOUR}),
        # a quotation is the document's words, so a verdict inside it is not the answer's
        ('The brief reads "Skylight is excluded from retail placements" and targets Acme Corp.', {SKYLIGHT}),
    ],
)
def test_campaign_descriptions_keep_their_campaign(text, expected):
    assert named(text) == expected


@pytest.mark.parametrize(
    "text",
    [  # review: invoices
        "- 0012_invoice — GS? no — total $17,195.51, not yet paid",
        "- 0012_invoice (total 17,195.51) is not yet paid.",
        "- Invoice 0012_invoice, total $17,195.51; due date is not stated.",
        "- 0012_invoice: $17,195.51 (tax not included)",
        "- 0012_invoice: $17,195.51 (excluding tax)",
        "- 0012_invoice: $17,195.51 excl. tax",
        "- 0012_invoice: $17,195.51, tax excluded",
        "- 0012_invoice: $17,195.51 — the PO number is not shown",
    ],
)
def test_invoice_details_keep_their_invoice(text):
    assert named_contracts(text, INVOICES, INVOICE_ALIASES) == {"0012_invoice"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A bare negated predicate is not a verdict: as often a description as a rejection. Accepted false keeps.
        ("Harbour Lights targets Acme Corp. Skylight does not target Acme Corp.", {HARBOUR, SKYLIGHT}),
        ("Harbour Lights targets Acme Corp. Skylight ne cible pas Acme Corp.", {HARBOUR, SKYLIGHT}),
        ("Only Harbour Lights. Skylight and Northstar target other clients.", set(CAMPAIGNS)),  # review
        (f"`{SID}` matches and `{EID}` does not.", {STAAR, EDIETS}),  # review: elliptical
        # A verdict reaches no earlier name over a clause.
        ("Skylight and Northstar have briefs too, but these are not for Acme Corp.", {SKYLIGHT, NORTHSTAR}),
        ("The search did not find a perpetual grant in Skylight or Northstar.", {SKYLIGHT, NORTHSTAR}),
        # A rejection in one sentence does not cancel a listing in another.
        ("Skylight is not an Acme campaign.\n\n| Campaign |\n|---|\n| Skylight |\n", {SKYLIGHT}),
    ],
)
def test_what_the_prose_rule_does_not_read_is_kept(text, expected):
    assert named_contracts(text, CAMPAIGNS + CONTRACTS, CAMPAIGN_ALIASES, IDS) == expected


def test_a_sentence_ends_before_a_backtick_or_a_lower_case_word():
    """review, item 2: "Staar: ... Papa: ... eDiets is not one." once ran as one sentence."""
    text = f"Staar (`{SID}`) has one. `{EID}` is not one. Papa (`{PID}`) has one. and `{EID}` was read."
    assert contracts(text) == {STAAR, PAPA, EDIETS}, "eDiets is listed without a verdict in its last sentence"
    assert contracts(f"Staar (`{SID}`) has one. `{EID}` is not one.") == {STAAR}
    # The one-name forms count the names in the sentence, so where it ends decides them.
    assert contracts(f"Staar (`{SID}`) has one. `{EID}` was read in full — excluded.") == {STAAR}
    assert named("Harbour Lights targets Acme Corp. skylight was read in full — excluded.") == {HARBOUR}


def test_a_label_naming_no_item_rejects_the_names_after_it_and_one_naming_an_item_is_read_by_the_patterns():
    assert named("- **Excluded near-misses:** Skylight (Jan 3), Northstar (Jan 9).") == set()
    assert named("Nothing was excluded: Skylight, Northstar.") == {SKYLIGHT, NORTHSTAR}
    assert named("Skylight: targets Acme Corp.") == {SKYLIGHT}
    # A label that names its item is a verdict on that item, not on the names after its colon.
    assert named("Skylight was excluded: only Harbour Lights targets Acme Corp.") == {HARBOUR}
    assert named("Harbour Lights targets Acme Corp.\nSkylight - excluded: it targets Northwind.") == {HARBOUR}


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


def test_the_rerun_cuad_key_16_reads_the_same_with_its_table_written_as_bullets():
    """review: the same words in the format another run would plausibly use, cells joined by an em dash."""
    answer = _fixture("fable-rerun-cuad-key-16.md")
    lines = []
    for line in answer.splitlines():
        if line.startswith(("|---", "| #", "| Contract")):
            continue
        if line.startswith("| "):
            line = "- " + " — ".join(cell.strip() for cell in line.strip("|").split("|"))
        lines.append(line)
    universe = sorted(set(KEY16_DOC_IDS.values()) | set(KEY16_POSITIVES))
    table = named_contracts(answer, universe, None, KEY16_DOC_IDS)
    assert named_contracts("\n".join(lines), universe, None, KEY16_DOC_IDS) == table
