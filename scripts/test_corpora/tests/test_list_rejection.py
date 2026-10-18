"""#816: what a list answer's section labels say about the items under them. A heading or margin lead-in that opens
with a reject phrase, or whose leading clause ends with one after only framing words, takes its section out; nothing
else does: not a label opening a line or bullet, not a denial of the category, not prose. A false drop silently
lowers recall on a right answer and a false keep only lowers precision, so where the two pull apart these pin the
keep. The three answers the issue names are fixtures, copied from the stored runs, and so are the probes of every
review of the earlier versions of this rule (marked "review"), each with its outcome under this one."""

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
        "Excluded from the final list",  # review 5: one word may stand before the list word
        "Not included in the final count",  # review 5
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
        "Delaware law - conflict rules excluded",  # "delaware", "law", "conflict" are not framing words
        "Delaware law — excluded",  # the reject phrase is after the leading clause
        "Rejected offers go to third parties",  # review 3
        "Rejected-offer matching rights",  # review 3
        "Excluded Products carve-out",  # review 3
        "Exclusion clauses",  # review 3
        "Exclusivity",  # review 3
        "Exclusive licenses",  # review 3
        "Rejection rights",  # review 3
        "Excluded by the key",  # review 3: "excluded by" what is not the list
        "Exclusions from the liability cap",  # review 5: "exclusions" before a preposition is read as a participle is
        "Rejected by the licensee",  # review 5
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
        "Exclusions",  # a policy's or a cap's exclusions are ordinary content (review 7)
        "Exclusions (uncapped)",  # review 7
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
    "label",
    [
        # A denial of a category is not read: it names a subtype as readily as the category (review 5).
        "Not a New York law contract (Delaware governs; New York is only the forum)",  # review 5
        "Not a liability insurance requirement (key-man life insurance only)",  # review 5
        "Not a post-termination non-disparagement clause (during the term only)",  # review 5
        "Not a right of first refusal (ROFN only)",  # review 4
        "Not a ROFR (rights of first negotiation)",  # review 3
        "Not a Delaware entity (Delaware law still governs)",  # review 3
        # ...so a denial of the question's own category is counted: accepted false keeps.
        "Not a non-disparagement clause (word appears only incidentally)",  # the rerun's cuad-key-16
        "B. Not a non-disparagement clause",
        "Not an Acme campaign",
        "Pas une clause de non-dénigrement",
        "Ce n’est pas une clause de non-dénigrement",
    ],
)
def test_a_denial_of_a_category_does_not_reject(label):
    assert not rejecting_label(label)


@pytest.mark.parametrize("label", ["Not a match", "Not a match for the question as asked", "Does not qualify"])
def test_a_reject_phrase_that_names_no_category_still_rejects(label):
    assert rejecting_label(label)


# ── sections and lines ──


def test_a_hedged_or_prohibiting_section_counts_and_a_rejecting_one_does_not():
    text = (
        "## Campaigns for Acme Corp\n\n- Harbour Lights\n\n"
        "## Borderline / related (mention Acme but not a classic Acme brief)\n\n- Skylight\n\n"
        "## Not an Acme campaign\n\n- Northstar\n"
    )
    assert named(text) == set(CAMPAIGNS), "a hedge counts, and so does a denial of the category"
    assert named(text.replace("Not an Acme campaign", "Excluded")) == {HARBOUR, SKYLIGHT}
    for heading in (
        "Irrevocable licenses (may not be revoked)",  # review 1
        "One-way (not mutual)",  # review 1
        "Insurance required (limits not listed)",  # review 2
        "ROFR (seller may sell once the offer is rejected)",  # review 2
        "Customer non-solicitation (employees excluded)",  # review 2
        "Delaware law (conflict-of-law rules excluded)",  # review 2
        "Third-party sale once the first offer is rejected",  # review 3
        "Not a ROFR (rights of first negotiation)",  # review 3
        "Not a New York law contract (Delaware governs; New York is only the forum)",  # review 5
    ):
        answer = (
            f"I found three.\n\n## Customer non-solicitation\n\n- `{SID}`\n\n## {heading}\n\n- `{EID}`\n- `{PID}`\n"
        )
        assert contracts(answer) == {STAAR, EDIETS, PAPA}, heading
    rejecting = f"Two.\n\n## Matches\n- `{EID}`\n\n## Excluded from consideration\n- `{SID}`\n"  # review 3
    assert contracts(rejecting) == {EDIETS}
    assert contracts(rejecting.replace("from consideration", "from the cap")) == {STAAR, EDIETS}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Probes of the line-label and label-only-line rules of earlier versions of #816, which are no longer
        # read: each outcome is what headings and margin lead-ins alone give, so most items are counted.
        (
            "Harbour Lights targets Acme Corp.\n- **Excluded near-misses:** Skylight (Jan 3), Northstar (Jan 9).",
            {HARBOUR, NORTHSTAR, SKYLIGHT},
        ),
        ("Harbour Lights targets Acme Corp.\nExcluded: Skylight, Northstar.", {HARBOUR, NORTHSTAR, SKYLIGHT}),
        ("Harbour Lights targets Acme Corp.\n- Not counted: Skylight.", {HARBOUR, SKYLIGHT}),
        ("Harbour Lights targets Acme Corp.\n- **Rejected** Skylight, Northstar.", {HARBOUR, NORTHSTAR, SKYLIGHT}),
        (f"Staar (`{SID}`) has one.\n**Not a match:** `{EID}`", {STAAR}),
        (
            "Harbour Lights targets Acme Corp.\nExcluded: Skylight.\nNorthstar also targets Acme Corp.",
            {HARBOUR, NORTHSTAR, SKYLIGHT},
        ),
        ("Harbour Lights targets Acme Corp.\nSkylight - excluded: it targets Northwind.", {HARBOUR, SKYLIGHT}),
        (
            f"Staar (`{SID}`) has one.\neDiets (`{EID}`) — excluded: the word appears only in a warranty.",
            {STAAR, EDIETS},
        ),
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
        ("Harbour Lights targets Acme Corp.\n- __Excluded:__ Skylight, Northstar.", {HARBOUR, NORTHSTAR, SKYLIGHT}),
        (f"Two.\n- `{EID}`\n- Excluded from consideration: `{SID}`", {EDIETS, STAAR}),  # review 3
        (
            f"Three.\n- `{EID}`\n- Seller may sell after the offer is rejected: `{SID}`, `{PID}`",
            set(CONTRACTS),
        ),  # review 3
        (f"Three.\n- `{EID}`\n- **Rejected-offer matching right:** Staar (`{SID}`)", {EDIETS, STAAR}),  # review 3
        (
            f"Two.\n\n- **Staar** (`{SID}`)\n  - Excluded: punitive damages, `{PID}` cross-reference",
            {PAPA, STAAR},
        ),  # review 3
        (
            "Harbour Lights.\nExcluded because they target Northwind and Polestar Industries rather than Acme Corp instead: Skylight.",
            {HARBOUR, SKYLIGHT},
        ),
        ("Harbour Lights.\nExcluded because they target Northwind: Skylight.", {HARBOUR, SKYLIGHT}),
        ("Skylight was excluded: only Harbour Lights targets Acme Corp.", {SKYLIGHT, HARBOUR}),
        (f"Two.\n- Not counted — Staar (`{SID}`): Papa John's (`{PID}`) qualifies.", {STAAR, PAPA}),
    ],
)
def test_line_label_probes(text, expected):
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
    [  # review 3, rewritten in review 8 so that each case is a heading or margin lead-in the escape alone keeps
        f"Two qualify.\n\n**Excluded:** none — both hits, Staar (`{SID}`) and Papa John's (`{PID}`), contain a genuine covenant.",
        f"Two qualify.\n\n**Exclusions:** none; Staar (`{SID}`) and Papa John's (`{PID}`) both qualify.".replace(
            "Exclusions", "Excluded"
        ),
        f"Two qualify.\n\n**Not counted:** nothing. The two are Staar (`{SID}`) and Papa John's (`{PID}`).",
        f"Two qualify.\n\n**False positives:** none, Staar (`{SID}`) and Papa John's (`{PID}`) are both real.",
        f"Two qualify.\n\n**Rejected:** none. Matches: Staar (`{SID}`), Papa John's (`{PID}`).",
        f"Two qualify.\n\n**Excluded:** n/a. Staar (`{SID}`) and Papa John's (`{PID}`).",
        f"Two qualify.\n\n**Rejected** — zero; Staar (`{SID}`), Papa John's (`{PID}`).",
        f"Two qualify.\n\n**Exclus :** aucun. Staar (`{SID}`), Papa John's (`{PID}`).",
        f"Two qualify.\n\n**Excluded:** nil, and Staar (`{SID}`) and Papa John's (`{PID}`) both qualify.",
        f"Two qualify.\n\n## False positives\n\nNone. Staar (`{SID}`) and Papa John's (`{PID}`) both qualify.",
        f"Two qualify.\n\n## Excluded: none\n\nStaar (`{SID}`) and Papa John's (`{PID}`) both qualify.",
        f"Two qualify.\n\n## Rejected\n\nNothing, but Staar (`{SID}`) and Papa John's (`{PID}`) both qualify.",
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
        f"One.\n- Staar (`{SID}`)\n\n**Excluded:** nothing in eDiets (`{EID}`) or Papa (`{PID}`) is a covenant.\n",
    ],
)
def test_none_of_and_a_bare_no_are_not_the_escape(text):
    assert contracts(text) == {STAAR}


def test_a_rejecting_label_not_followed_by_none_still_rejects():
    assert contracts(f"Two.\n\n- Papa John's (`{PID}`)\n\n**Excluded:** Staar (`{SID}`), warranty only.") == {PAPA}
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


def test_fable_synthetic_key_8_counts_the_invoice_its_bulleted_label_excludes():
    """bench-20261015-0032-synthetic-fable: twelve February invoices, then "- **Excluded near-misses:** ... `0050`
    (GS-2025-00847, Jan 3) ... were not included". 0050 is named by its unique invoice number. A bulleted bold label
    is not a lead-in and line labels are not read, so 0050 is counted and the answer scores 0.96, as on main: an
    accepted false keep. ("0048 is a July 2025 invoice — excluded", the line the issue quotes, names 0048 by no form
    the key holds.)"""
    answer = _fixture("fable-synthetic-key-8.md")
    universe = sorted(KEY8_ALIASES)
    entry = {"kind": "list", "positives": KEY8_POSITIVES, "universe": universe, "aliases": KEY8_ALIASES}
    assert named_contracts(answer, universe, KEY8_ALIASES) == set(KEY8_POSITIVES) | {"0050_invoice"}
    assert score(entry, answer)["score"] == pytest.approx(24 / 25)
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


def test_fable_rerun_cuad_key_16_counts_its_borderline_section():
    """bench-20261015-2259-cuad-fable-rerun: three headings. "Borderline / related provisions (contain disparagement
    language but not a classic non-disparagement covenant)" holds Berkshire Hills, Cybergy and Digital Cinema, CUAD
    positives, which #814's rule dropped ("borderline" and "not a"): 0.43. "Not a non-disparagement clause" holds
    eDiets, which a denial of the category would reject; denials are not read, so it is counted (an accepted false
    keep): 0.63."""
    answer = _fixture("fable-rerun-cuad-key-16.md")
    universe = sorted(set(KEY16_DOC_IDS.values()) | set(KEY16_POSITIVES))
    got = named_contracts(answer, universe, None, KEY16_DOC_IDS)
    berkshire, cybergy, cinema = KEY16_POSITIVES[:3]
    assert {berkshire, cybergy, cinema} <= got
    assert KEY16_DOC_IDS["3c40cba1-a4cc-47f4-a1a6-2011dc83888a"] in got, "a denial is not read"
    entry = {"kind": "list", "positives": KEY16_POSITIVES, "universe": universe}
    result = score(entry, answer, KEY16_DOC_IDS)
    assert (result["named"], result["right"]) == (10, 6) and result["score"] == pytest.approx(12 / 19)


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
    assert table == set(KEY16_DOC_IDS.values()), "every contract it names"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Probes of the line-label and label-only-line rules of earlier versions of #816, which are no longer
        # read: each outcome is what headings and margin lead-ins alone give, so most items are counted.
        (
            f"One.\n\n- Staar (`{SID}`)\n- **Excluded near-misses:**\n  - eDiets (`{EID}`)\n  - Papa (`{PID}`)\n",
            {EDIETS, PAPA, STAAR},
        ),
        (f"One.\n\n- Staar (`{SID}`)\n\nExcluded:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {EDIETS, PAPA, STAAR}),
        (
            f"One.\n\n- Staar (`{SID}`)\n\n__Rejected:__\n1. eDiets (`{EID}`)\n2. Papa (`{PID}`)\n",
            {EDIETS, PAPA, STAAR},
        ),
        (f"One.\n\n- Staar (`{SID}`)\n\n**Not counted**:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {STAAR}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\n\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\n## Matches\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\nMatches:\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\n- **Matches:**\n  - Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\nAnd one more that qualifies:\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Two.\n\nExcluded:\n- eDiets (`{EID}`)\nPapa (`{PID}`) qualifies.\n", {EDIETS, PAPA}),
        (f"Two.\n\n- **Excluded near-misses:**\n  - eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Two.\n\n  - **Excluded:**\n    - eDiets (`{EID}`)\n  - Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"One.\n\nExcluded:\n- eDiets (`{EID}`)\n  - Papa (`{PID}`)\n- Staar (`{SID}`)\n", {EDIETS, PAPA, STAAR}),
        (f"Three.\n\nMatches:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n- Staar (`{SID}`)\n", {STAAR, EDIETS, PAPA}),
        (f"Three.\n\nExcluded from the cap:\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"Three.\n\nExcluded: none.\n- eDiets (`{EID}`)\n- Papa (`{PID}`)\n", {EDIETS, PAPA}),
        (f"One.\n- Staar (`{SID}`)\n- Excluded: no covenant in eDiets (`{EID}`), only a warranty.\n", {EDIETS, STAAR}),
        (
            f"Two.\n\n- Staar (`{SID}`): $5M CGL\n- Punitive damages excluded: Papa John's (`{PID}`) must carry $2M CGL\n",
            {STAAR, PAPA},
        ),
        (
            f"Two.\n\n- Staar (`{SID}`)\n\n### Employee solicitation not included\n\n- Papa John's (`{PID}`)\n",
            {STAAR, PAPA},
        ),
        (
            f"Two.\n\n## Rights of first refusal\n\n- Staar (`{SID}`)\n\n"
            f"## Not a right of first refusal (right of first negotiation only)\n\n- Papa John's (`{PID}`), s7\n",
            {STAAR, PAPA},
        ),
    ],
)
def test_review_4_label_line_probes(text, expected):
    assert contracts(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Probes of the line-label and label-only-line rules of earlier versions of #816, which are no longer
        # read: each outcome is what headings and margin lead-ins alone give, so most items are counted.
        (
            f"Found two.\n\n## Matches\n\n- `{SID}`\n\n## Exclusions: none\n\n- `{PID}` also qualifies (§9)\n",
            {STAAR, PAPA},
        ),
        (
            f"Found two.\n\n## Matches\n\n- `{SID}`\n\n**Exclusions:** none\n\n- `{PID}` also qualifies (§9)\n",
            {STAAR, PAPA},
        ),
        (f"Found two.\n\n## Matches\n\n- `{SID}`\n\n## Exclusions: eDiets\n\n- `{EID}`\n", {EDIETS, STAAR}),
        (f"Not counted: `{EID}`; counted: `{SID}`\n", {EDIETS, STAAR}),
        (f"Excluded contracts aside, the matches are: `{SID}`, `{PID}`\n", {STAAR, PAPA}),
        (f"Excluded: `{EID}`. Matches: `{SID}`.\n", {EDIETS, STAAR}),
        (f"Excluded: `{EID}` Matches: `{SID}`\n", {EDIETS, STAAR}),
        (f"Excluded: `{EID}`. `{SID}` qualifies.\n", {EDIETS, STAAR}),
        (f"- **Not counted — Staar (`{SID}`)**: Papa John's (`{PID}`) qualifies.\n", {STAAR, PAPA}),
        (f"Excluded as all of them were for other firms and not Acme at all: `{EID}`\n", {EDIETS}),
        (f"Excluded as all of them were for other firms: `{EID}`\n", {EDIETS}),
        (f"- **Excluded:** `{EID}`; `{SID}` qualifies.\n", {EDIETS, STAAR}),
        (f"- **Excluded:** `{EID}`, `{PID}` (warranties only).\n", {EDIETS, PAPA}),
        (f"Excluded - see below: `{EID}`\n", {EDIETS}),
        (f"Found.\n\nMatches\n-------\n\n- `{SID}`\n\nExcluded\n--------\n\n- `{EID}`\n", {STAAR, EDIETS}),
        (f"Two.\n\n- `{SID}`\n\nExcluded contracts aside, these remain:\n- `{EID}`\n", {STAAR, EDIETS}),
        (
            f"Two.\n\n- `{SID}`\n\nNotable Contracts That Were Close But Do NOT Qualify:\n- `{EID}`\n- `{PID}`\n",
            {EDIETS, PAPA, STAAR},
        ),
        (
            f"Two.\n\n- `{SID}`\n\nThese contracts were reviewed carefully by me in full but in the end were also excluded:\n- `{EID}`\n",
            {STAAR, EDIETS},
        ),
    ],
)
def test_review_5_probes(text, expected):
    assert contracts(text) == expected


STAAR_LINE = f"**Staar** (`{SID}`): $5M product liability."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Probes of the line-label and label-only-line rules of earlier versions of #816, which are no longer
        # read: each outcome is what headings and margin lead-ins alone give, so most items are counted.
        (
            f"Three.\n\n- {STAAR_LINE}\n  Exclusions:\n  - punitive damages\n- **eDiets** (`{EID}`): $1M CGL.\n"
            f"- **Papa John's** (`{PID}`): $2M CGL.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Three.\n\n1. {STAAR_LINE}\n   **Exclusions:**\n   - punitive damages\n2. **eDiets** (`{EID}`): $1M CGL.\n"
            f"3. **Papa John's** (`{PID}`): $2M CGL.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Three.\n\n1. {STAAR_LINE}\n   - **Exclusions:**\n     - punitive damages\n2. **eDiets** (`{EID}`): $1M CGL.\n"
            f"3. **Papa John's** (`{PID}`): $2M CGL.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Three.\n\n- Staar (`{SID}`)\n  Excluded:\n  - conflict-of-law rules\n- eDiets (`{EID}`)\n"
            f"- Papa John's (`{PID}`)\n",
            {STAAR, EDIETS, PAPA},
        ),
        (f"Found.\n\n2. Papa `{PID}`\n   Exclusions:\n   - consequential damages\n3. eDiets `{EID}`\n", {PAPA, EDIETS}),
        (
            f"Two.\n\n- Staar (`{SID}`)\n  Excluded:\n  - eDiets (`{EID}`)\n    - Papa John's (`{PID}`)\n",
            {EDIETS, PAPA, STAAR},
        ),
        (
            f"Three.\n\nExcluded because all of these were for other firms and not for Acme at all:\n"
            f"- `{EID}`\n- `{PID}`\n- `{SID}`\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Three.\n\nExcluded because all of these were for other firms:\n- `{EID}`\n- `{PID}`\n- `{SID}`\n",
            {EDIETS, PAPA, STAAR},
        ),
        (
            f"Found two.\n\n## Matches\n\n- `{SID}`\n- **Not included above but also qualifies:** `{PID}`\n",
            {STAAR, PAPA},
        ),
        (f"Found two.\n\n- `{SID}`\n\n**Not counted above, but qualifies:** `{PID}`\n", {STAAR, PAPA}),
        (f"Found two.\n\n- `{SID}`\n- **Not included above but does not qualify:** `{PID}`\n", {PAPA, STAAR}),
        (f"One: `{SID}`.\n- **Excluded:** eDiets, doc_id: `{EID}` (warranty only)\n", {EDIETS, STAAR}),
        (f"One: `{SID}`.\n- **Excluded:** eDiets — Doc ID: `{EID}`\n", {EDIETS, STAAR}),
        (
            f"One: `{SID}`.\n- **Not counted:** Papa John's International, Inc. and eDiets.com, Inc. (`{EID}`)\n",
            {EDIETS, PAPA, STAAR},
        ),
        (f"One: `{SID}`.\n- **Excluded:** the U.S. affiliate of eDiets (`{EID}`)\n", {EDIETS, STAAR}),
        (f"One: `{SID}`.\n- **Excluded:** Agreement No. 7, eDiets (`{EID}`)\n", {EDIETS, STAAR}),
        (f"Two.\n- **Excluded:** `{EID}` (doc 2: warranty only); `{SID}` qualifies.\n", {EDIETS, STAAR}),
        (f"Two.\n- **Excluded near-misses:** eDiets (reviewed: warranty only) and `{PID}`\n", {PAPA}),
        (f"Found two.\n\n## Matches\n\n- `{SID}`\n\n## Excluded\n\nZero-rated: `{EID}`\n", {STAAR}),
        (f"Found two.\n\n- `{SID}`\n\n## Excluded\n\nNil-rated contracts are out of scope: `{EID}`\n", {STAAR}),
        (f"Found two.\n\n- `{SID}`\n\n## Excluded\n\nNone — `{EID}` qualifies too.\n", {STAAR, EDIETS}),
    ],
)
def test_review_6_probes(text, expected):
    assert contracts(text) == expected


def test_review_6_campaign_shapes():
    assert named("Harbour Lights.\nExcluded: Skylight: targets Northwind.") == {HARBOUR, SKYLIGHT}
    assert named("Harbour Lights.\nExcluded: Skylight (Jan. 3) and Northstar (Jan. 9).") == set(CAMPAIGNS)
    assert named("Harbour Lights.\n\n**Excluded:** Skylight (Jan. 3) and Northstar (Jan. 9).") == {HARBOUR}


@pytest.mark.parametrize(
    ("label", "rejects"),
    [
        ("Not included above but also qualifies", False),
        ("Not counted above, but qualifies", False),
        ("Excluded, but it applies", True),  # "applies" is said of the clause as readily as of the item (review 8)
        ("Rejected at first, though it counts", False),
        ("Excluded, yet it is a match", False),
        ("Excluded, yet a match", True),  # "a match" alone is the keyword's (review 8)
        ("Not counted, mais it qualifies", False),
        ("Notable Contracts That Were Close But Do NOT Qualify", True),
        ("Not included above but does not qualify", True),
        ("Excluded, but it doesn't count", True),
        ("Excluded, but see the note", True),
    ],
)
def test_a_contrast_that_affirms_qualification_does_not_reject(label, rejects):
    assert rejecting_label(label) is rejects


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # review 7: a lead-in is judged on its whole label, the bold words and any words after them up to a colon.
        (f"Three.\n\n- `{EID}`\n\n**Excluded** from the cap: indemnity claims under Staar (`{SID}`).", {EDIETS, STAAR}),
        (f"Two.\n\n- `{EID}`\n\n**Not counted** above, but qualifies: Staar (`{SID}`)", {EDIETS, STAAR}),
        (
            f"Two.\n\n- `{EID}`\n\n**Not included** in my first pass, but it qualifies: Staar (`{SID}`).",
            {EDIETS, STAAR},
        ),
        (
            f"Two.\n\n## Matches\n- `{EID}`\n\n**Excluded** from the cap under Staar (`{SID}`): indemnity.\n- `{PID}`\n",
            {EDIETS, STAAR, PAPA},
        ),
        (f"Two.\n\n- `{EID}`\n\n**Excluded** from the list: Staar (`{SID}`).", {EDIETS}),
        (f"Two.\n\n- `{EID}`\n\n**Excluded**\n\nStaar (`{SID}`)", {EDIETS}),
        # A bold span followed by running words with no colon is a sentence, not a lead-in.
        (f"Three.\n\n- `{EID}`\n\n**Rejected** offers go to third parties under Staar (`{SID}`).", {EDIETS, STAAR}),
        (
            f"Two.\n\n- `{EID}`\n\n**Excluded** territories aside, Staar (`{SID}`) bars soliciting customers.",
            {EDIETS, STAAR},
        ),
        (
            f"Three.\n\n## Matches\n- `{EID}`\n\n**Rejected** offers revert to the licensor under Staar (`{SID}`), s4.\n"
            f"- `{PID}` also qualifies\n",
            {EDIETS, STAAR, PAPA},
        ),
        (
            f"Three.\n\n## Matches\n- `{EID}`\n\n**Rejected offers revert to the licensor:** Staar (`{SID}`), s4.\n"
            f"- `{PID}` also qualifies\n",
            {EDIETS, STAAR, PAPA},
        ),
        # The same shapes in bullets are not lead-ins, and line labels are not read.
        (f"Three.\n- `{EID}`\n- **Excluded** from the cap: indemnity claims under Staar (`{SID}`).", {EDIETS, STAAR}),
        (f"Three.\n- `{EID}`\n- **Rejected** offers go to third parties under Staar (`{SID}`).", {EDIETS, STAAR}),
        (f"Two.\n- `{EID}`\n- **Not counted** above, but qualifies: Staar (`{SID}`)", {EDIETS, STAAR}),
        (
            f"Two.\n- `{EID}`\n- **Excluded** territories aside, Staar (`{SID}`) bars soliciting customers.",
            {EDIETS, STAAR},
        ),
        # review 7: "Exclusions" is ordinary content, in a line or a lead-in.
        (
            f"Three.\n\n1. Staar (`{SID}`): $5M CGL.\n2. eDiets (`{EID}`): $1M.\n"
            f"Exclusions: Papa John's (`{PID}`) policy excludes punitive damages.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Three.\n\n- Staar (`{SID}`)\n- eDiets (`{EID}`)\n- Exclusions (uncapped): Papa John's (`{PID}`) indemnity.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Three.\n\n- Staar (`{SID}`)\n- eDiets (`{EID}`)\n\n**Exclusions:** Papa John's (`{PID}`) policy excludes punitive damages.\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            f"Found.\n\n- Staar (`{SID}`)\n- Not included: Canada and Mexico, per Papa John's (`{PID}`) s2.\n",
            {STAAR, PAPA},
        ),
        # review 7: an indented bold lead-in over a paragraph is not read (an accepted false keep: read, an indented
        # "**Excluded:**" inside a list item would take the rest of the section).
        (f"One.\n\n- `{SID}`\n\n  **Not counted:**\n  eDiets (`{EID}`) warranty only.\n", {STAAR, EDIETS}),
        # A heading that says "none" after its own colon rejects nothing (review 5; "Exclusions" no longer rejects, so
        # the probe is "Excluded").
        (
            f"Found two.\n\n## Matches\n\n- `{SID}`\n\n## Excluded: none\n\n- `{PID}` also qualifies (§9)\n",
            {STAAR, PAPA},
        ),
        (f"Found two.\n\n## Matches\n\n- `{SID}`\n\n## Excluded: eDiets\n\n- `{EID}`\n", {STAAR}),
        # review 7: the rest.
        (
            f"Two.\n\n| Contract | Note |\n|---|---|\n| Staar (`{SID}`) | ok |\n| Excluded: | `{EID}` |\n",
            {STAAR, EDIETS},
        ),
        (f"One.\n\n## Matches\n- `{SID}`\n\n## Excluded\n\nNone, but eDiets (`{EID}`) was close.\n", {STAAR, EDIETS}),
        (f"Two.\n\n- `{SID}`\n\n## Excluded (keyword hits, but none count)\n- `{EID}`\n", {STAAR}),
        (f"Two.\n\n- `{SID}`\n\n## Excluded (from the cap)\n- `{EID}`\n", {STAAR, EDIETS}),
        (f"Two.\n\n1. **Matches**\n   - `{SID}`\n2. **Excluded**\n   - `{EID}`\n", {STAAR, EDIETS}),
        (f"One: `{SID}`.\n- Excluded: Rand, eDiets, etc. Papa (`{PID}`) qualifies.\n", {STAAR, PAPA}),
        (f"Excluded:\n- `{EID}`\n\nMatches:\n- `{SID}`\n", {STAAR, EDIETS}),
        (f"Two.\n- `{SID}`\n- **Excluded per the question's scope (employees only):** `{EID}`", {STAAR, EDIETS}),
        (f"Two.\n- `{SID}`\n- Excluded as additional insureds: Papa John's affiliates (`{PID}`)", {STAAR, PAPA}),
        (f"Two.\n- `{SID}`\n- Excluded per s4.2: consequential damages, Papa John's (`{PID}`)", {STAAR, PAPA}),
        (f"Two.\n- `{SID}`\n* Rejected: `{EID}`", {STAAR, EDIETS}),
        (f"Two.\n\t- `{SID}`\n\tExcluded:\n\t- `{EID}`\n- `{PID}`", {STAAR, EDIETS, PAPA}),
    ],
)
def test_review_7_probes(text, expected):
    assert contracts(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # review 8: a section ends at any bold-led line, as on main, whether or not that line is a lead-in.
        (
            "I found two.\n\n"
            f"**1. Staar Surgical** (`{SID}`) — §4 non-disparagement covenant\n\n"
            f"**Not counted:** eDiets (`{EID}`) — the word appears only in warranties.\n\n"
            f"**2. Papa John's** (`{PID}`) — §9 mutual non-disparagement\n",
            {STAAR, PAPA},
        ),
        (
            f"Two.\n\n**Staar** (`{SID}`): §4.\n\n**Not counted:** eDiets (`{EID}`), warranties only.\n\n"
            f"**Also qualifying** — Papa John's (`{PID}`), §9.\n",
            {STAAR, PAPA},
        ),
        (
            f"Two.\n\n- Staar (`{SID}`)\n\n**Not counted:** eDiets (`{EID}`).\n\n  **Papa John's** (`{PID}`): §9 qualifies.\n",
            {STAAR, PAPA},
        ),
        # ...while only a heading or a margin lead-in's whole label can reject one.
        (f"Two.\n\n- Staar (`{SID}`)\n\n  **Not counted:**\n  eDiets (`{EID}`)\n", {STAAR, EDIETS}),
        # review 8: a bold span followed by a colon or a dash is a whole label.
        (f"Two.\n\n- `{SID}`\n\n**Excluded** — Papa John's (`{PID}`), recitals only.\n", {STAAR}),
        (f"Two.\n\n- `{SID}`\n\n**Not counted** – eDiets (`{EID}`), warranties only.\n", {STAAR}),
        (f"Two.\n\n- `{SID}`\n\n**Not counted**: eDiets (`{EID}`), warranties only.\n", {STAAR}),
        (f"Two.\n\n- `{SID}`\n\n**Excluded** territories aside, eDiets (`{EID}`) bars soliciting.\n", {STAAR, EDIETS}),
        # review 8: contract nouns after "from" are the contract's, not the list's.
        (
            "Two contracts carve indemnity out of the cap.\n\n"
            f"**Excluded from the total liability cap:** indemnity under Staar (`{SID}`) and Papa John's (`{PID}`).\n\n"
            f"**Capped:** eDiets (`{EID}`).\n",
            {STAAR, EDIETS, PAPA},
        ),
        (
            "Two licenses carve out trademarks.\n\n## Licensed IP\nSoftware and documentation.\n\n"
            f"## Excluded from the license scope\n- Staar (`{SID}`): trademarks\n- Papa John's (`{PID}`): marks\n",
            {STAAR, PAPA},
        ),
        # review 8: a contrast past the leading clause does not save it.
        (
            f"One.\n\n## Delaware-governed\n- Staar (`{SID}`)\n\n"
            f"## Near-miss (Delaware is named, but only the forum clause applies)\n- eDiets (`{EID}`)\n",
            {STAAR},
        ),
    ],
)
def test_review_8_probes(text, expected):
    assert contracts(text) == expected


@pytest.mark.parametrize(
    ("label", "rejects"),
    [
        # review 8: the affirming contrast is read in the leading clause only.
        ("Not counted (Delaware forum, but New York law applies)", True),
        ("Excluded: mentions Delaware, but New York law applies", True),
        ("Near-misses (the keyword matches, but the clause applies only to employees)", True),
        ("Excluded (but the keyword matches)", True),
        ("Reviewed and excluded, though one match is close", True),
        ("Excluded (non-solicit applies to employees but not customers)", True),
        ("Rejected — the covenant applies only during the term, but the question asks post-term", True),
        ("Excluded — but it counts toward the other list", True),
        ("Excluded; however, the word appears", True),
        ("Excluded, but it matches the keyword", True),
        ("Excluded but included in the appendix", True),
        ("Not included above but also qualifies", False),
        ("Not counted above, but qualifies", False),
        ("Not counted, but qualifies under a broader reading", False),
        ("Excluded, but none of these qualifies", True),  # "none" is a negation after the contrast
        ("Excluded, but nothing here counts", True),
        # review 8: "count", "total", "scope", "review" are the answer's only at the end of the label.
        ("Excluded from the total liability cap", False),
        ("Excluded from the license scope", False),
        ("Excluded from the scope of the license", False),
        ("Excluded from the review period", False),
        ("Not included in the license scope", False),
        ("Not counted toward the total purchase commitment", False),
        ("Excluded from the count of business days", False),
        ("Excluded from the final list", True),
        ("Not included in the final count", True),
        ("Excluded from the count", True),
        ("Excluded from the count:", True),
        ("Not counted toward the total", True),
        ("Excluded from scope", True),
        # review 8: roman numerals are read past.
        ("III. Excluded contracts", True),
        ("IV) Rejected", True),
        ("I. Excluded contracts", True),
        ("Iv. Excluded", True),
    ],
)
def test_review_8_labels(label, rejects):
    assert rejecting_label(label) is rejects


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # review 9: a spaced ASCII hyphen is a dash, after "none" and after a bold label; an unspaced one is not.
        ("Harbour Lights.\n\n**Excluded:** none - Skylight and Northstar both target Acme.", set(CAMPAIGNS)),
        ("Harbour Lights.\n\n**Excluded:** none — Skylight and Northstar both target Acme.", set(CAMPAIGNS)),
        ("Harbour Lights.\n\n**Excluded** - Skylight, Northstar", {HARBOUR}),
        ("Harbour Lights.\n\n**Excluded** — Skylight, Northstar", {HARBOUR}),
        ("Harbour Lights.\n\n**Excluded:** zero-rated Skylight", {HARBOUR}),
        ("Harbour Lights.\n\n**Excluded:** none-such Skylight", {HARBOUR}),
        ("Harbour Lights.\n\n**Excluded**-Skylight", {HARBOUR, SKYLIGHT}),
    ],
)
def test_review_9_probes(text, expected):
    assert named(text) == expected


@pytest.mark.parametrize(
    ("label", "rejects"),
    [
        # review 9: "non-matching", "exclusions" as framing, "the count above", "for lack of".
        ("Non-matching", True),
        ("Non-matches", True),
        ("Non-match", True),
        ("Rejected-offer matching rights", False),
        ("Exclusions and near-misses", True),
        ("Exclusions", False),
        ("Excluded from the count above", True),
        ("Excluded from the total below", True),
        ("Excluded from the review here", True),
        ("Excluded from the count of days above", False),
        ("Excluded for not meeting the criteria", True),
        ("Rejected for lack of a covenant", True),
        ("Excluded for failing the test", True),
        ("Excluded for failure to qualify", True),
        ("Excluded for the indemnity", False),
        # review 9: "counts as" and "is a match only by keyword" are not the item qualifying.
        ("Excluded, though it counts as related", True),
        ("Not counted, but it is a match only by keyword", True),
        ("Not counted, but it is a match merely in name", True),
        ("Rejected, but it counts", False),
        ("Not counted, but it is a match", False),
    ],
)
def test_review_9_labels(label, rejects):
    assert rejecting_label(label) is rejects
