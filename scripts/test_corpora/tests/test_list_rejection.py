"""#816: what a list answer's labels say about the items under them. A heading, lead-in or line label that opens
with a reject phrase, or whose leading clause ends with one, takes its items out; nothing else does, and prose is not
read for rejection at all. A false drop silently lowers recall on a right answer and a false keep only lowers
precision, so where the two pull apart these pin the keep. The three answers the issue names are fixtures, copied
from the stored runs, and so are the probes of both reviews of earlier versions of this rule (marked "review")."""

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


# ── labels ──


@pytest.mark.parametrize(
    "label",
    [
        # Opens with a reject phrase (each observed in a stored answer unless marked).
        "False positive (excluded)",
        "Excluded — near-misses worth noting",
        "Excluded near-misses:",
        "Near-misses (reviewed and excluded)",
        "Near-miss worth flagging (not Delaware-governed)",
        "Not counted:",  # not observed
        "Not included",  # not observed
        "Not a match:",
        "Non-matches",  # not observed
        "Rejected",  # not observed
        "Ruled out",  # not observed
        "Does not qualify",  # not observed
        "Exclusions",  # not observed
        "Excluded from the list",  # not observed
        "Faux positifs",  # not observed, French
        "Non retenus",  # not observed, French
        "Exclus",  # not observed, French
        # Opens with a denial of the category.
        "Not a non-disparagement clause (word appears only incidentally)",
        "Not an Acme campaign",  # not observed
        "Ce n'est pas une clause de non-dénigrement",  # not observed, French
        # Its leading clause ends with a reject phrase.
        "Reviewed and excluded",
        "Reviewed and excluded (matched the keywords but are not license grants)",
        "Candidates reviewed and excluded",
        "Checked and excluded (not a *customer* non-solicit)",
        "Contracts Excluded (Delaware mentioned but NOT the governing law)",
        "Context on what was excluded:",
        "⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)",
        "Contrats écartés",  # not observed, French
        # Markup, emoji and numbering in front of the words are read past.
        "**1. Excluded**",
        "⛔ Rejected candidates",
        "B. Not counted",
    ],
)
def test_a_label_that_opens_with_or_whose_leading_clause_ends_with_a_reject_phrase_rejects(label):
    assert rejecting_label(label)


@pytest.mark.parametrize(
    "label",
    [
        # Hedges count: the decision #816 made.
        "Borderline / related provisions (contain disparagement language but not a classic non-disparagement covenant)",
        "Borderline / special case",
        "Mentions / borderline (included for completeness, not operative ROFRs in the document itself)",
        "Related but narrower: trademark / licensed-material use restrictions (3)",
        "Possible matches (not confirmed)",  # not observed
        # A reject phrase later in the label, after the leading clause, does nothing.
        "One additional keyword match, not counted above:",
        "Mention insurance but contain **no** maintenance requirement (excluded after reading)",
        "Insurance required (limits not listed)",  # review 2
        "ROFR (seller may sell once the offer is rejected)",  # review 2
        "Customer non-solicitation (employees excluded)",  # review 2
        "Delaware law (conflict-of-law rules excluded)",  # review 2
        "ROFR (offer rejected first)",  # review 2
        # A negation is not a reject phrase: the clauses asked for are prohibitions.
        "Irrevocable licenses (may not be revoked)",  # review 1
        "Licenses that can't be revoked",  # review 1
        "Perpetual licenses (do not expire)",  # review 1
        "Contracts in which a party shall not disparage the other",  # review 1
        "Contracts where a party may not solicit the other's customers",  # review 1
        "Clauses prohibiting solicitation (not limited to employees)",  # review 1
        "Mutual clauses (neither party may disparage)",  # review 1
        "Delaware law (not New York)",  # review 1
        "Insurance required (amount not specified)",  # review 1
        "One-way, not mutual",  # review 1
        "Not mutual",  # review 1: "not" without an article is not a denial of the category
        "Not Found:",
        "⚠️ Contracts With Employee-Only Non-Solicitation Clauses (NOT Customer Solicitation Bans)",
        "Contracts with Non-Solicitation Clauses Limited to Employees (Not Customers):",
        "Licensee not selected by the Licensor",  # review 2
        "Coverage amount not listed",  # review 2
        "Perpetual only (not described as irrevocable)",
        "Contracts with a genuine non-disparagement clause (a party covenants not to disparage the other)",
        # A denied degree or form, or a denial that goes on.
        "Not a classic covenant, but related",
        "Not a party but bound",  # review 2
        # A leading clause that opens with a negation.
        "Nothing excluded",  # review 1
        "Not excluded",  # review 1
        "None rejected",  # review 1
        "No false positives",  # review 1
        "Not a near-miss",  # review 1
        "Nothing has been excluded",  # review 2
        "No contracts were excluded",  # review 2
        "None were excluded",  # review 2
        "None of these were excluded",  # review 2
        # A leading clause that opens as a condition, and "excluded from" what is not the list.
        "If the first offer is rejected",  # review 2
        "Excluded from the cap",  # review 2
        # Not a reject phrase at all.
        "Excluding duplicates",  # review 1
        "Clear matches",  # review 1
        "Disparagement as a termination trigger only — no affirmative covenant (1)",
        "Matches (no duplicates)",
        "Exclusive license grants (2)",
        "Contracts governed by Delaware law — 8 found",
    ],
)
def test_any_other_label_keeps_its_items(label):
    assert not rejecting_label(label)


def test_the_cost_a_leading_clause_ending_in_a_reject_participle_about_something_else_rejects():
    """Accepted so that "Reviewed and excluded" and synthetic-key-20's "Context on what was excluded" reject."""
    assert rejecting_label("Clauses with consequential damages excluded")


# ── sections and lines ──


def test_a_hedged_or_prohibiting_section_counts_and_a_rejecting_one_does_not():
    text = (
        "## Campaigns for Acme Corp\n\n- Harbour Lights\n\n"
        "## Borderline / related (mention Acme but not a classic Acme brief)\n\n- Skylight\n\n"
        "## Not an Acme campaign\n\n- Northstar\n"
    )
    assert named(text) == {HARBOUR, SKYLIGHT}
    for heading in (
        "Irrevocable licenses (may not be revoked)",  # review 1
        "One-way (not mutual)",  # review 1
        "Insurance required (limits not listed)",  # review 2
        "ROFR (seller may sell once the offer is rejected)",  # review 2
        "Customer non-solicitation (employees excluded)",  # review 2
        "Delaware law (conflict-of-law rules excluded)",  # review 2
    ):
        answer = (
            f"I found three.\n\n## Customer non-solicitation\n\n- `{SID}`\n\n## {heading}\n\n- `{EID}`\n- `{PID}`\n"
        )
        assert contracts(answer) == {STAAR, EDIETS, PAPA}, heading


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Harbour Lights targets Acme Corp.\n- **Excluded near-misses:** Skylight (Jan 3), Northstar (Jan 9).",
            {HARBOUR},
        ),
        ("Harbour Lights targets Acme Corp.\nExcluded: Skylight, Northstar.", {HARBOUR}),
        ("Harbour Lights targets Acme Corp.\n- Not counted: Skylight.", {HARBOUR}),
        ("Harbour Lights targets Acme Corp.\n- **Rejected** Skylight, Northstar.", {HARBOUR}),
        (f"Staar (`{SID}`) has one.\n**Not a match:** `{EID}`", {STAAR}),
        # The label covers its own line only.
        (
            "Harbour Lights targets Acme Corp.\nExcluded: Skylight.\nNorthstar also targets Acme Corp.",
            {HARBOUR, NORTHSTAR},
        ),
        # A label that names its item is not a label over the line: the line is kept.
        ("Harbour Lights targets Acme Corp.\nSkylight - excluded: it targets Northwind.", {HARBOUR, SKYLIGHT}),
        (
            f"Staar (`{SID}`) has one.\neDiets (`{EID}`) — excluded: the word appears only in a warranty.",
            {STAAR, EDIETS},
        ),
        # Labels that keep their line.
        ("Nothing was excluded: Skylight, Northstar.", {SKYLIGHT, NORTHSTAR}),  # review 1
        (f"No contracts were excluded: `{SID}`, `{PID}`.", {STAAR, PAPA}),  # review 2
        (f"Nothing has been excluded: `{SID}`, `{PID}`.", {STAAR, PAPA}),  # review 2
        (f"None of these were excluded: `{SID}`, `{PID}`.", {STAAR, PAPA}),  # review 2
        (f"- If the first offer is rejected: Staar (`{SID}`) may sell to a third party.", {STAAR}),  # review 2
        (f"- Excluded from the cap: indemnity claims under Staar (`{SID}`).", {STAAR}),  # review 2
        (
            f"I found three.\n\n- `{EID}`: $1M CGL.\n- **Coverage amount not listed:** `{SID}`, `{PID}`.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (f"I found three.\n\n- `{EID}`: $1M CGL.\n- Amounts not listed: `{SID}`, `{PID}`.\n", {STAAR, EDIETS, PAPA}),
        (f"I found two.\n\n**Licensee not selected by the Licensor:**\n- `{SID}`\n- `{PID}`\n", {STAAR, PAPA}),
        ("Skylight: targets Acme Corp.", {SKYLIGHT}),
    ],
)
def test_a_line_label_that_rejects_and_names_no_item_takes_the_rest_of_its_line(text, expected):
    assert named_contracts(text, CAMPAIGNS + CONTRACTS, CAMPAIGN_ALIASES, IDS) == expected


# ── prose is not read ──


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Rejections in prose are counted: accepted false keeps (the stored answers held two, RaeSystems below).
        ("Harbour Lights targets Acme Corp.\nSkylight is not a match.", {HARBOUR, SKYLIGHT}),
        ("Harbour Lights targets Acme Corp.\nSkylight — excluded.", {HARBOUR, SKYLIGHT}),
        ("Harbour Lights targets Acme Corp.\nI excluded Skylight.", {HARBOUR, SKYLIGHT}),
        (
            "Harbour Lights targets Acme Corp.\nSkylight is not an Acme campaign: it targets Northwind.",
            {HARBOUR, SKYLIGHT},
        ),
        ("The campaign is Harbour Lights rather than Skylight.", {HARBOUR, SKYLIGHT}),
        ("Only Harbour Lights. Skylight and Northstar target other clients.", set(CAMPAIGNS)),  # review 1
        (f"Staar (`{SID}`) matches, eDiets (`{EID}`) is not a match.", {STAAR, EDIETS}),  # review 1
        (f"`{SID}` matches and `{EID}` does not.", {STAAR, EDIETS}),  # review 1
        ("Papa John's International Inc. is not a match.", {PAPA}),  # review 2
        # Descriptions of the clause, which the earlier versions read as rejections, are of course counted.
        (
            f"- Staar (`{SID}`), §4.2: if the offer is rejected, STAAR may sell to a third party on the same terms.",
            {STAAR},
        ),  # review 1
        (f"- Staar (`{SID}`): product liability insurance of $5M; punitive damages excluded.", {STAAR}),  # review 1
        (
            f"- Staar (`{SID}`): Distributor must insure the Products, with consequential losses excluded from cover.",
            {STAAR},
        ),  # review 1
        (
            f"1. Staar Surgical (`{SID}`) — Section 9 requires insurance, but the amount is not specified.",
            {STAAR},
        ),  # review 1
        (
            f"- `{SID}` (Staar): the license is perpetual, although it is not described as irrevocable.",
            {STAAR},
        ),  # review 1
        (f"Staar (`{SID}`): insurance is required — this isn't optional.", {STAAR}),  # review 1
        (f"- `{SID}` - Distributor shall not solicit customers of `{PID}`.", {STAAR, PAPA}),  # review 1
        (f'Staar (`{SID}`) is not a "classic" non-disparagement clause, but it contains one.', {STAAR}),  # review 2
        (
            f"- Papa John's (`{PID}`) is not a Delaware corporation, but the agreement selects Delaware law.",
            {PAPA},
        ),  # review 2
        (
            f"- eDiets (`{EID}`) is not a party to any franchise; §6 bars soliciting the other's customers.",
            {EDIETS},
        ),  # review 2
        (
            f"- Staar (`{SID}`) — rejected offers may then be made to third parties on the same terms.",
            {STAAR},
        ),  # review 2
        (f"- Staar (`{SID}`): rejected offers revert to STAAR after 30 days.", {STAAR}),  # review 2
        (f"- `{SID}` — Staar; conflict rules excluded: Delaware law governs.", {STAAR}),  # review 2
        (f"| `{SID}` | Staar | — excluded |", {STAAR}),  # review 2
        (f"Staar (`{SID}`) Section 4.2 Distributor is not a franchisee.", {STAAR}),  # review 2
        (f"Two contracts: `{SID}` and `{PID}`. I excluded none of them.", {STAAR, PAPA}),  # review 2
        (f"- `{SID}` - excluded territories aside, Distributor may not solicit customers.", {STAAR}),  # review 2
        (f"- Staar (`{SID}`) — excluded from the cap: indemnity, so liability is uncapped.", {STAAR}),  # review 2
        (f"Staar (`{SID}`) was not excluded: its §6.7 is a real covenant.", {STAAR}),  # review 1
        (f"I did not exclude Staar (`{SID}`).", {STAAR}),  # review 1
        ("Harbour Lights targets Acme Corp, but the budget is not set.", {HARBOUR}),  # review 1
        ("After excluding duplicates, the campaigns are Harbour Lights and Skylight.", {HARBOUR, SKYLIGHT}),
    ],
)
def test_prose_is_not_read_for_rejection(text, expected):
    assert named_contracts(text, CAMPAIGNS + CONTRACTS, CAMPAIGN_ALIASES, IDS) == expected


@pytest.mark.parametrize(
    "text",
    [  # review 1 and 2: invoice details
        "- 0012_invoice — $17,195.51 (excluding tax)",
        "- 0012_invoice: $17,195.51, tax excluded",
        "- 0012_invoice: $17,195.51 (tax not included)",
        "- 0012_invoice (total 17,195.51) is not yet paid.",
        "- 0012_invoice — $17,195.51, VAT-excluded.",
        "- 0012_invoice: $17,195.51 — tax-excluded",
        "- 0012_invoice — $17,195.51 (non-excluded)",
    ],
)
def test_invoice_details_keep_their_invoice(text):
    assert named_contracts(text, INVOICES, INVOICE_ALIASES) == {"0012_invoice"}


def test_the_stored_prose_rejection_the_rule_no_longer_reads_is_counted():
    """bench-20260928-1050-keyed, Sonnet 4.6, cuad-key-17: dropped by the second version, counted now (accepted)."""
    rae = "RaeSystemsInc_20001114_10-Q_EX-10.57_2631790_EX-10.57_Co-Branding Agreement"
    text = "The RaeSystemsInc Co-Branding Agreement only has *revocable* licenses, so I'll exclude it."
    assert named_contracts(text, [rae, STAAR]) == {rae}


# ── the three answers #816 names ──


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_fable_synthetic_key_20_counts_only_harbour_lights():
    """bench-20261015-0032-synthetic-fable: "Answer: Only one campaign — Harbour Lights", then Skylight and
    Northstar under "**Context on what was excluded:**". The lead-in (#814's rule, kept) is what takes them out."""
    answer = _fixture("fable-synthetic-key-20.md")
    entry = {"kind": "list", "positives": [HARBOUR], "universe": CAMPAIGNS, "aliases": CAMPAIGN_ALIASES}
    assert named(answer) == {HARBOUR} and score(entry, answer)["score"] == 1.0
    # The cost, stated: the prose after the lead-in ("— none target Acme Corp") is not read, so without the lead-in
    # both are counted.
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
    # The cost, stated: named by its stem, the line the issue quotes is prose, which is not read, so it is counted.
    rejected = answer.replace("0048 is a July", "0048_invoice is a July")
    assert "0048_invoice" in named_contracts(rejected, universe, KEY8_ALIASES)


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
    eDiets = KEY16_DOC_IDS["3c40cba1-a4cc-47f4-a1a6-2011dc83888a"]
    assert table == set(KEY16_DOC_IDS.values()) - {eDiets}, "every contract it names but the one it rejects"
