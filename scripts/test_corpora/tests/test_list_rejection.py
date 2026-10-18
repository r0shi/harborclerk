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
# The keyed questions, as `questions/keyed/*.yaml` asks them: a denial of the category is read against these.
Q15 = "Which contracts contain a clause prohibiting the solicitation of the other party's customers?"
Q16 = "Which contracts contain a non-disparagement clause? List every one you can find."
Q18 = "Which contracts contain a right of first refusal, first offer or first negotiation? List every one you can find."
Q19 = "Which contracts contain a requirement that a party maintain insurance? List every one you can find."
Q20 = "Which contracts are governed by Delaware law? List every one you can find."
Q_CAMPAIGNS = "Which campaigns have a marketing brief that targets Acme Corp?"
Q16_FR = "Quels contrats contiennent une clause de non-dénigrement ?"
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
        "Excluded from consideration",  # review 3
        "Excluded from scope",  # not observed
        "Excluded from review",  # not observed
        "Excluded from the tally",  # not observed
        "Excluded from the total",  # not observed
        "Excluded from my count",  # review 3
        "Not included in the list",  # review 3
        "Non-match (excluded from the tally)",  # review 3
        "Not a match for the question as asked",  # review 3
        "EXCLUDED",  # review 3
        "Exclus de la liste",  # review 3, French
        "Contrats non retenus",  # review 3, French
        "Écartés",  # review 3, French
        "Doesn’t qualify",  # not observed: a typographic apostrophe
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
        "Rejected after reading",  # not observed: an adverbial after an opening participle
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
        "Not a typical ROFR (a right of first negotiation)",  # not observed
        "Not an express covenant",  # not observed
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
        # A reject phrase after a condition or attachment word in the leading clause (review 3), or a participle used
        # before a noun, or the first half of a compound, or the singular "exclusion".
        "Third-party sale once the first offer is rejected",
        "Seller may sell after the offer is rejected",
        "Customer non-solicits with employees excluded",
        "Insurance with deductibles excluded",
        "Delaware law with conflict rules excluded",
        "Clauses with consequential damages excluded",  # pinned as a cost in the version before
        "Delaware law - conflict rules excluded",  # a spaced hyphen ends the leading clause
        "Delaware law — excluded",  # the reject phrase is after the leading clause
        "Rejected offers go to third parties",  # review 3
        "Rejected-offer matching rights",  # review 3
        "Excluded Products carve-out",  # review 3
        "Exclusion clauses",  # review 3
        "Exclusivity",  # review 3
        "Exclusive licenses",  # review 3
        "Rejection rights",  # review 3
        "Excluded by the key",  # review 3: "excluded by" what is not the list
        "Not included in the cap",  # review 3
        # Accepted false keeps (review 3): the reject phrase is not where the rule reads it.
        "Not relevant / excluded",
        "Also reviewed, not counted",
        "Not qualifying",
        "Not one of them",
        # A label of more words than a label has, or a denial with no question to read it against (see below).
        "Not a non-disparagement clause (word appears only incidentally)",
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


@pytest.mark.parametrize("word", ["once", "after", "when", "whenever", "if", "unless", "until", "provided", "with",
                                  "without", "where", "except"])  # fmt: skip
def test_a_closing_phrase_after_a_condition_describes_the_clause_and_does_not_reject(word):
    assert not rejecting_label(f"Sale to a third party {word} the offer is rejected")
    assert rejecting_label("Reviewed and rejected") and rejecting_label("Context on what was excluded")


@pytest.mark.parametrize("word", ["si", "lorsque", "quand", "avec", "sans"])
def test_and_in_french(word):
    assert not rejecting_label(f"Vente à un tiers {word} l'offre rejetée")
    assert rejecting_label("Examinés et rejetés")


@pytest.mark.parametrize(
    ("label", "rejects"),
    [
        # Every word before a closing reject phrase names the items or frames them: a verdict.
        ("Context on what was excluded", True),
        ("Reviewed and excluded", True),
        ("Checked and excluded (not a *customer* non-solicit)", True),
        ("Candidates reviewed and excluded", True),
        ("Contracts Excluded (Delaware mentioned but NOT the governing law)", True),
        ("⚠️ Notable Contracts That Were Close But Do NOT Qualify (Employee Non-Solicitation Only)", True),
        ("Notable Near-Misses (NOT governed by Delaware law)", True),
        ("The remaining documents were also rejected", True),
        ("Examinés et rejetés", True),
        # Any other word: the phrase describes something in the clause (review 4).
        ("Punitive damages excluded", False),
        ("Employee solicitation not included", False),
        ("Consequential damages excluded", False),
        ("Clauses with consequential damages excluded", False),
        ("Contracts whose renewals are excluded", False),
        # An opening participle followed by a reason is a verdict (review 4).
        ("Not counted toward the total", True),
        ("Excluded due to scope", True),
        ("Excluded following review", True),
        ("Exclus car hors sujet", True),
    ],
)
def test_a_closing_phrase_rejects_only_after_words_that_name_or_frame_the_items(label, rejects):
    assert rejecting_label(label) is rejects


@pytest.mark.parametrize(
    ("label", "question", "rejects"),
    [
        # A denial rejects only when the denied phrase's last content word is one of the question's.
        ("Not a non-disparagement clause (word appears only incidentally)", Q16, True),
        ("B. Not a non-disparagement clause", Q16, True),  # review 3: numbering read past
        ("Not an Acme campaign", Q_CAMPAIGNS, True),
        ("Pas une clause de non-dénigrement", Q16_FR, True),  # review 3, French
        ("Ce n’est pas une clause de non-dénigrement", Q16_FR, True),  # a typographic apostrophe
        ("Not a ROFR (rights of first negotiation)", Q18, False),  # review 3: shares nothing with the question
        ("Not a ROFR", Q18, False),
        ("Not a Delaware entity (Delaware law still governs)", Q20, False),  # review 3
        ("Not a Delaware entity", Q20, False),  # the last content word, "entity", is not the question's
        ("Not a minimum-coverage requirement (amount unspecified)", Q19, False),  # review 3
        ("Not a right of first refusal — a right of first offer", Q18, False),  # review 3: it says what it is
        # Several alternatives: a denial of one leaves the item free to qualify under another (review 4).
        ("Not a right of first refusal", Q18, False),
        ("Not a right of first refusal (right of first negotiation only)", Q18, False),  # review 4
        ("Not a right of first refusal (ROFN only)", Q18, False),  # review 4
        ("Not a right of first offer", Q18, False),  # review 4
        ("Not a first-refusal right; a first negotiation right", Q18, False),  # review 4
        ("Not a right of first refusal, first offer or first negotiation", Q18, True),
        ("Not a ROFR, ROFO or ROFN", Q18, True),
        ("Not a first-refusal, first-offer or first-negotiation right", Q18, True),
        (
            "Not an irrevocable or perpetual license",
            "Which contracts contain an irrevocable or perpetual license grant?",
            True,
        ),
        ("Not an irrevocable license", "Which contracts contain an irrevocable or perpetual license grant?", False),
        ("Not a Delaware law clause", Q20, True),  # review 4: one alternative, named
        ("Not an insurance requirement per se", Q19, False),  # review 4: "per se" is what is denied
        ("Not an exclusive license, but perpetual", Q16, False),  # review 3
        ("Not a party but bound", Q15, False),  # review 2
        ("Not a classic non-disparagement covenant", Q16, False),  # a degree or a form
        ("Not a typical ROFR", Q18, False),
        ("Not a non-disparagement clause", None, False),  # no question, no denial
    ],
)
def test_a_denial_rejects_only_the_questions_own_category(label, question, rejects):
    assert rejecting_label(label, question) is rejects


# ── sections and lines ──


def test_a_hedged_or_prohibiting_section_counts_and_a_rejecting_one_does_not():
    text = (
        "## Campaigns for Acme Corp\n\n- Harbour Lights\n\n"
        "## Borderline / related (mention Acme but not a classic Acme brief)\n\n- Skylight\n\n"
        "## Not an Acme campaign\n\n- Northstar\n"
    )
    assert named_contracts(text, CAMPAIGNS, CAMPAIGN_ALIASES, None, Q_CAMPAIGNS) == {HARBOUR, SKYLIGHT}
    assert named(text) == set(CAMPAIGNS), "without the question the denial is not read"
    for heading in (
        "Irrevocable licenses (may not be revoked)",  # review 1
        "One-way (not mutual)",  # review 1
        "Insurance required (limits not listed)",  # review 2
        "ROFR (seller may sell once the offer is rejected)",  # review 2
        "Customer non-solicitation (employees excluded)",  # review 2
        "Delaware law (conflict-of-law rules excluded)",  # review 2
        "Third-party sale once the first offer is rejected",  # review 3
        "Not a ROFR (rights of first negotiation)",  # review 3, under Q18 below
    ):
        answer = (
            f"I found three.\n\n## Customer non-solicitation\n\n- `{SID}`\n\n## {heading}\n\n- `{EID}`\n- `{PID}`\n"
        )
        assert named_contracts(answer, CONTRACTS, None, IDS, Q18) == {STAAR, EDIETS, PAPA}, heading
    rejecting = f"Two.\n\n## Matches\n- `{EID}`\n\n## Excluded from consideration\n- `{SID}`\n"  # review 3
    assert contracts(rejecting) == {EDIETS}
    assert contracts(rejecting.replace("from consideration", "from the cap")) == {STAAR, EDIETS}


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
        ("Harbour Lights targets Acme Corp.\n- __Excluded:__ Skylight, Northstar.", {HARBOUR}),
        (f"Two.\n- `{EID}`\n- Excluded from consideration: `{SID}`", {EDIETS}),  # review 3
        (
            f"Three.\n- `{EID}`\n- Seller may sell after the offer is rejected: `{SID}`, `{PID}`",
            set(CONTRACTS),
        ),  # review 3
        (f"Three.\n- `{EID}`\n- **Rejected-offer matching right:** Staar (`{SID}`)", {EDIETS, STAAR}),  # review 3
        (
            f"Two.\n\n- **Staar** (`{SID}`)\n  - Excluded: punitive damages, `{PID}` cross-reference",
            {STAAR},
        ),  # review 3
        # A label longer than a label is not one: the line is kept.
        (
            "Harbour Lights.\nExcluded because they target Northwind and Polestar Industries instead: Skylight.",
            {HARBOUR, SKYLIGHT},
        ),
        ("Harbour Lights.\nExcluded because they target Northwind: Skylight.", {HARBOUR}),
        # A label that names its own item and rejects keeps the line, so the items after its colon still count (and
        # the named one is counted too: the label text stays).
        ("Skylight was excluded: only Harbour Lights targets Acme Corp.", {SKYLIGHT, HARBOUR}),
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


@pytest.mark.parametrize(
    "text",
    [  # review 3: a rejecting label that says it rejects nothing keeps what follows it
        f"I searched for non-disparagement language.\n\n**Excluded:** none — both hits, Staar (`{SID}`) and Papa John's (`{PID}`), contain a genuine covenant.",
        f"Two contracts qualify.\n- **Exclusions:** none; Staar (`{SID}`) and Papa John's (`{PID}`) both qualify.",
        f"Two contracts qualify.\nNot counted: nothing. The two are Staar (`{SID}`) and Papa John's (`{PID}`).",
        f"Two contracts qualify.\nFalse positives: none, Staar (`{SID}`) and Papa John's (`{PID}`) are both real.",
        f"Rejected: none. Matches: Staar (`{SID}`), Papa John's (`{PID}`).",
        f"Two.\n\n## Results\n\n- **Rejected offers go to third parties:** Staar (`{SID}`), Papa John's (`{PID}`)",
        f"Two.\n- Excluded: n/a. Staar (`{SID}`) and Papa John's (`{PID}`).",
        f"Two.\n- Rejected: zero — Staar (`{SID}`), Papa John's (`{PID}`).",
        f"Two.\n- Exclus : aucun. Staar (`{SID}`), Papa John's (`{PID}`).",
        f"Two.\n\n## False positives\n\nNone. Staar (`{SID}`) and Papa John's (`{PID}`) both qualify.",
    ],
)
def test_a_rejecting_label_followed_by_none_rejects_nothing(text):
    assert contracts(text) == {STAAR, PAPA}


@pytest.mark.parametrize(
    "text",
    [  # review 4: "none of ...", and a bare "No <noun>", are the rejection, not its absence
        f"One.\n\n## Matches\n\n- Staar (`{SID}`)\n\n## Excluded\n\nNone of these contains a genuine covenant:\n"
        f"- eDiets (`{EID}`)\n- Papa (`{PID}`)\n",
        f"One.\n\n## Matches\n\n- Staar (`{SID}`)\n\n## Rejected candidates\n\nNo affirmative covenant in either:\n"
        f"- eDiets (`{EID}`)\n- Papa (`{PID}`)\n",
        f"One.\n\n- Staar (`{SID}`)\n\n**Excluded:** none of these is a real covenant: eDiets (`{EID}`), Papa (`{PID}`).\n",
        f"One.\n\n## Matches\n- Staar (`{SID}`)\n\n## Near-misses\n\nNo operative clause; recitals only:\n"
        f"- eDiets (`{EID}`)\n- Papa (`{PID}`)\n",
        f"One.\n\n## Matches\n- Staar (`{SID}`)\n\n## Excluded\n\n### Nominal mentions\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n",
        f"One.\n- Staar (`{SID}`)\n- Excluded: nothing in eDiets (`{EID}`) or Papa (`{PID}`) is a covenant.\n",
    ],
)
def test_none_of_and_a_bare_no_are_not_the_escape(text):
    assert named_contracts(text, CONTRACTS, None, IDS, Q16) == {STAAR}


def test_a_rejecting_label_not_followed_by_none_still_rejects():
    assert contracts(f"Two.\n- **Exclusions:** Staar (`{SID}`)\n- Papa John's (`{PID}`)") == {PAPA}
    assert contracts(f"Two.\n\n## False positives\n\nStaar (`{SID}`).\n\n## Matches\n\nPapa (`{PID}`)") == {PAPA}


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
    got = named_contracts(answer, universe, None, KEY16_DOC_IDS, Q16)
    berkshire, cybergy, cinema = KEY16_POSITIVES[:3]
    assert {berkshire, cybergy, cinema} <= got
    assert KEY16_DOC_IDS["3c40cba1-a4cc-47f4-a1a6-2011dc83888a"] not in got, "eDiets is under the rejecting heading"
    entry = {"kind": "list", "positives": KEY16_POSITIVES, "universe": universe}
    result = score(entry, answer, KEY16_DOC_IDS, Q16)
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
    table = named_contracts(answer, universe, None, KEY16_DOC_IDS, Q16)
    assert named_contracts("\n".join(lines), universe, None, KEY16_DOC_IDS, Q16) == table
    eDiets = KEY16_DOC_IDS["3c40cba1-a4cc-47f4-a1a6-2011dc83888a"]
    assert table == set(KEY16_DOC_IDS.values()) - {eDiets}, "every contract it names but the one it rejects"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # review 4: a line that is only a rejecting label takes out the list under it.
        (
            f"One.\n\n- Staar (`{SID}`)\n- **Excluded near-misses:**\n  - eDiets (`{EID}`)\n  - Papa (`{PID}`)\n",
            {STAAR},
        ),
        (f"One.\n\n- Staar (`{SID}`)\n\nExcluded:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {STAAR}),
        (f"One.\n\n- Staar (`{SID}`)\n\n__Rejected:__\n1. eDiets (`{EID}`)\n2. Papa (`{PID}`)\n", {STAAR}),
        (f"One.\n\n- Staar (`{SID}`)\n\n**Not counted**:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {STAAR}),
        # Its list ends at a blank line, a heading, another label line, a line that is not an item, or (under a
        # bulleted label) an item back at the label's indentation.
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\n\n- Papa (`{PID}`)\n", {PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\n## Matches\n- Papa (`{PID}`)\n", {PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\nMatches:\n- Papa (`{PID}`)\n", {PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\nAnd one more that qualifies:\n- Papa (`{PID}`)\n", {PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\nPapa (`{PID}`) qualifies.\n", {PAPA}),
        (f"Two.\n\n- **Excluded near-misses:**\n  - eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {PAPA}),
        (f"Two.\n\n  - **Excluded:**\n    - eDiets (`{EID}`)\n  - Papa (`{PID}`)\n", {PAPA}),
        # A plain label's list runs at any indentation.
        (f"One.\n\nExcluded:\n- eDiets (`{EID}`)\n  - Papa (`{PID}`)\n- Staar (`{SID}`)\n", set()),
        # A label line that keeps, or is not only a label, takes nothing.
        (f"Three.\n\nMatches:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n- Staar (`{SID}`)\n", {STAAR, EDIETS, PAPA}),
        (f"Three.\n\nExcluded from the cap:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Three.\n\nExcluded: none.\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        # review 4: a descriptive closing phrase on a line label takes nothing.
        (
            f"Two.\n\n- Staar (`{SID}`): $5M CGL\n- Punitive damages excluded: Papa John's (`{PID}`) must carry $2M CGL\n",
            {STAAR, PAPA},
        ),
        (
            f"Two.\n\n- Staar (`{SID}`)\n\n### Employee solicitation not included\n\n- Papa John's (`{PID}`)\n",
            {STAAR, PAPA},
        ),
        # review 4: a denial of one alternative keeps its section.
        (
            f"Two.\n\n## Rights of first refusal\n\n- Staar (`{SID}`)\n\n"
            f"## Not a right of first refusal (right of first negotiation only)\n\n- Papa John's (`{PID}`), s7\n",
            {STAAR, PAPA},
        ),
    ],
)
def test_review_4_shapes(text, expected):
    question = Q18 if "first refusal" in text else Q19 if "CGL" in text else Q15 if "solicitation" in text else Q16
    assert named_contracts(text, CONTRACTS, None, IDS, question) == expected
