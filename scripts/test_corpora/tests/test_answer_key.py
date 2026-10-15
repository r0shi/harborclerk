"""The answer key scores an answer against human labels. Its matching is crude on purpose; these pin exactly how
crude, because a scorer that is wrong punishes the model that was right."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.corpora import cuad_key
from scripts.test_corpora.runner.answer_key import date_spellings, named_contracts, opening, score

KEYED = Path(cuad_key.__file__).resolve().parent.parent / "questions" / "keyed"
STAAR = "StaarSurgicalCompany_20180801_10-Q_EX-10.37_11289449_EX-10.37_Distributor Agreement"
EDIETS = "EdietsComInc_20001030_10QSB_EX-10.4_2606646_EX-10.4_Co-Branding Agreement"
ACME_A = "AcmeHoldingsInc_20010101_10-K_EX-10.1_1_EX-10.1_Supply Agreement"
ACME_B = "AcmeHoldingsInc_20020101_10-K_EX-10.2_2_EX-10.2_License Agreement"
SHORT = "Abc_20010101_10-K_EX-10.1_3_EX-10.1_Service Agreement"
UNIVERSE = [STAAR, EDIETS, ACME_A, ACME_B, SHORT]


def test_a_fact_needs_every_group_in_any_of_its_spellings():
    entry = {"kind": "fact", "all_of": [["ExxonMobil", "Exxon Mobil"], ["FuelCell Energy"]]}
    assert score(entry, "The parties are Exxon-Mobil Research and FUELCELL ENERGY, Inc.")["score"] == 1.0
    assert score(entry, "FuelCell Energy is one party.")["score"] == 0.5
    assert score(entry, "")["score"] == 0.0 and score(entry, None)["score"] == 0.0


def test_an_answer_that_opens_by_saying_the_fact_is_absent_scores_0_whatever_it_mentions_after():
    """cuad-key-7 in bench-20260924-0724-keyed (#713), as the issue quotes it: qwen3-8b said the law was not
    stated, listed a party "in Nevada", and guessed at Nevada. The key is Nevada; the scorer gave it 1.0 and the
    judge failed it, and the judge was right."""
    nevada = {"kind": "fact", "all_of": [["Nevada"]]}
    absent = (
        "The governing law of the Cybergy Holdings affiliate agreement is not explicitly stated in the retrieved "
        "excerpts. The parties are Cybergy Holdings, Inc. and Mount Knowledge Holdings Inc. in Nevada. The "
        "governing law might default to the state of incorporation of one of the parties (e.g., U.S. Virgin "
        "Islands or Nevada)."
    )
    assert score(nevada, absent) == {"score": 0.0, "groups_found": 0, "groups": 1, "absent": "not explicitly stated"}
    assert opening(absent).endswith("in the retrieved excerpts")
    # A plain correct answer still scores 1.0; so does one that says something else is absent, later.
    assert score(nevada, "The agreement is governed by the laws of the State of Nevada.")["score"] == 1.0
    assert score(nevada, "**Nevada.** The venue, however, is not explicitly stated.")["score"] == 1.0
    assert score(nevada, "\n\nNevada law governs\r\nThe venue is not explicitly stated.")["score"] == 1.0
    # The date matcher is the same kind of check, and gets the same guard; the phrasings are quality.py's.
    absent_date = {"kind": "date", "date": "2022-03-15"}
    assert score(absent_date, "I could not find the agreement date; the term ran from March 15, 2022.") == {
        "score": 0.0,
        "absent": "could not find",
    }
    assert score(absent_date, "March 15, 2022. I could not find an expiry date.")["score"] == 1.0


def test_the_opening_is_the_first_sentence_past_a_label_a_list_marker_or_a_reasoning_block():
    """Review of #764: the #713 answer came back behind a bold label and a list number, and "Inc." ended the
    sentence early. A thinking model's block, kept in the answer, is not its opening either."""
    nevada = {"kind": "fact", "all_of": [["Nevada"]]}
    said_absent = "The governing law is not explicitly stated in the excerpts. A party is based in Nevada."
    for shape in (
        f"**Answer:**\n{said_absent}",
        f"**Answer**:\n{said_absent}",
        f"1. {said_absent}",
        f"- {said_absent}",
        f"<think>Let me look for Nevada.</think>\n{said_absent}",
    ):
        assert opening(shape) == "The governing law is not explicitly stated in the excerpts", shape
        assert score(nevada, shape)["score"] == 0.0, shape
    # A bold line or a heading without a colon is the answer, not a label (second review of #764: read past, the
    # caveat after it zeroed a correct answer). Beside the label case, so a change to the rule is a decision.
    caveat = "The venue is not explicitly stated."
    for shape in (
        f"**Nevada.**\n{caveat}",
        f"**Nevada**\n\n{caveat}",
        f"**Governing law: Nevada**\n{caveat}",
        f"## Nevada\n{caveat}",
    ):
        assert "not explicitly stated" not in opening(shape), shape
        assert score(nevada, shape)["score"] == 1.0, shape
    assert opening(
        "The parties are Cybergy Holdings, Inc. and Mount Knowledge Holdings Inc. in Nevada. Law: none."
    ) == ("The parties are Cybergy Holdings, Inc. and Mount Knowledge Holdings Inc. in Nevada")
    assert opening("Governed by U.S. law (e.g., Nevada). The venue is not explicitly stated.") == (
        "Governed by U.S. law (e.g., Nevada)"
    )
    # An abbreviation before a capital does end the sentence early; the check reads less and is the weaker for it.
    assert (
        opening("Incorporated in the U.S. Virgin Islands. The law is not explicitly stated.")
        == "Incorporated in the U.S"
    )
    # A reasoning block that says the fact is absent is not the answer's opening; the answer after it is.
    assert score(nevada, "<think>The law is not explicitly stated anywhere.</think>\nNevada.")["score"] == 1.0
    assert opening("**Nevada.** The venue, however, is not explicitly stated.") == "**Nevada"


def test_what_the_opening_rule_costs_is_a_decision():
    """The rule is dumb on purpose, and these are its two known costs, pinned so a change to them is a decision:
    a caveat about something else in the first sentence zeroes a correct answer, and an answer that states the
    parties first and the absence second is credited for a keyed term it mentions in passing."""
    nevada = {"kind": "fact", "all_of": [["Nevada"]]}
    assert score(nevada, "Although the venue is not explicitly stated, the agreement is governed by Nevada law.") == {
        "score": 0.0,
        "groups_found": 0,
        "groups": 1,
        "absent": "not explicitly stated",
    }
    assert score(nevada, "I could not find a venue clause, but the governing law is Nevada.")["score"] == 0.0
    parties_first = (
        "The parties are Cybergy Holdings, Inc. and Mount Knowledge Holdings Inc. in Nevada. The governing law is "
        "not explicitly stated in the retrieved excerpts."
    )
    assert score(nevada, parties_first)["score"] == 1.0
    # A heading, or a bold line without a colon, is kept as the opening, since `## Nevada` and `**Nevada**` are
    # answers: the #713 shape under a topic heading or a colon-less label is the miss the scorer had before the
    # rule, not a new one (reviews of #764 and #771).
    said_absent = "The governing law is not explicitly stated in the excerpts. A party is in Nevada."
    for label, shape in (
        ("## Governing Law", f"## Governing Law\n{said_absent}"),
        ("**Answer**", f"**Answer**\n{said_absent}"),
    ):
        assert opening(shape) == label, shape
        assert score(nevada, shape)["score"] == 1.0, shape


@pytest.mark.parametrize(
    "text",
    [
        "effective as of March 15, 2022.",
        "on the 15th of March 2022",
        "15 March 2022",
        "Mar. 15, 2022",
        "2022-03-15",
        "3/15/2022",
        "03/15/22",
        "entered into effective as of Mar. 15th, 2022",
    ],
)
def test_a_date_is_found_however_an_american_contract_writes_it(text):
    assert score({"kind": "date", "date": "2022-03-15"}, text)["score"] == 1.0


def test_a_zero_padded_day_is_the_same_day():
    """Coherus writes "Nov. 02, 2019". The first version of the matcher did not accept it, and the generator's
    check against the contract's own text is what showed it."""
    assert (
        score({"kind": "date", "date": "2019-11-02"}, "effective as of Nov. 02, 2019 (the Effective Date)")["score"]
        == 1.0
    )
    assert "nov022019" in date_spellings("2019-11-02") and "nov22019" in date_spellings("2019-11-02")


@pytest.mark.parametrize(
    "text",
    ["March 16, 2022", "March 15, 2021", "13/15/2022", "3/15/20221", "23/15/2022", "15/3/2022", "in March 2022"],
)
def test_another_date_is_not_it(text):
    """Day-first numeric dates are not accepted: 3/4/2022 would be two days, and the corpus is American."""
    assert score({"kind": "date", "date": "2022-03-15"}, text)["score"] == 0.0


@pytest.mark.parametrize(
    "text", ["dated 12 November 2019", "22 November 2019", "on 12 Nov 2019", "file 120191102", "id 201911021"]
)
def test_a_date_inside_another_number_is_not_it(text):
    """Review of #697: "2 November 2019" matched inside "12 November 2019", and the ISO form inside any digit run."""
    assert score({"kind": "date", "date": "2019-11-02"}, text)["score"] == 0.0
    assert score({"kind": "date", "date": "2019-11-02"}, "dated 2 November 2019")["score"] == 1.0
    # A citation marker whose id starts with a digit is not part of the date (gpt-oss-20b writes exactly this).
    assert (
        score({"kind": "date", "date": "2020-02-04"}, "dated **February\u202f4,\u202f2020**【141b4469-c5c0】")["score"]
        == 1.0
    )


def test_a_contract_is_named_by_its_whole_title_or_by_a_filer_with_one_contract_in_the_text():
    """In the text, never from the answer's `citations`: those are every hit the tools returned (review of #697)."""
    typographic = EDIETS.replace("-", "‑")  # how a model renders it; the first bake-off met exactly this
    assert named_contracts(f"| 1 | *{typographic}* | p. 17 |", UNIVERSE) == {EDIETS}
    assert named_contracts("the Staar Surgical Company distributor agreement", UNIVERSE) == {STAAR}
    # Two contracts from one filer: its name alone says neither.
    assert named_contracts("Acme Holdings Inc has such a clause", UNIVERSE) == set()
    # A filer name under eight characters is too likely to be a word.
    assert named_contracts("the ABC agreement", UNIVERSE) == set()
    assert named_contracts("", UNIVERSE) == set()


def test_a_list_is_scored_by_f1_so_that_recall_shows():
    entry = {"kind": "list", "positives": [STAAR, EDIETS, ACME_A, ACME_B], "universe": UNIVERSE}
    one = score(entry, "Only one: Staar Surgical Company.")
    assert (one["named"], one["right"], one["precision"], one["recall"]) == (1, 1, 1.0, 0.25)
    assert one["score"] == pytest.approx(0.4), "right as far as it goes, and a quarter of the answer"
    everything = score(entry, " ".join(UNIVERSE))
    assert (everything["precision"], everything["recall"]) == (0.8, 1.0) and everything["score"] == pytest.approx(8 / 9)
    assert score(entry, "No contract in the corpus has such a clause.")["score"] == 0.0
    assert score({**entry, "positives": []}, "Staar Surgical Company")["score"] == 0.0
    with pytest.raises(ValueError, match="unknown key entry kind"):
        score({"kind": "essay"}, "x")


ID_STAAR = "ebbbd23d-3b0c-4c0d-a8c8-ac81198f6105"
ID_EDIETS = "9311d183-07d1-41f4-8cd9-3cbfc712769c"
ID_ELSEWHERE = "d60be7c8-b461-466c-beb3-b6f39fb77778"
DOC_IDS = {ID_STAAR: STAAR, ID_EDIETS: EDIETS, ID_ELSEWHERE: "Other_20000101_10-K_EX-10.1_9_EX-10.1_Lease", "7": ACME_A}


def test_a_contract_is_named_by_its_document_id_when_the_map_resolves_it():
    """bench-20261014-2328-cuad-fable: "SteelVault Corp – Equidata Marketing Affiliate Agreement (doc_id
    `ebbbd23d-…`)", never the filename stem; four of six lists scored 0 named. The map is the citations the
    answer's tools returned, read only for the ids the answer wrote out."""
    by_id = f"Two contracts: **SteelVault – Equidata** (doc_id `{ID_STAAR}`) and Women.com (`{ID_EDIETS}`, p. 13)."
    assert named_contracts(by_id, UNIVERSE, None, DOC_IDS) == {STAAR, EDIETS}
    assert named_contracts(by_id, UNIVERSE) == set(), "without the map the ids are noise, as before"
    # An id the map does not hold names nothing; one mapped to a title outside the universe names nothing.
    assert named_contracts("see `c84261cc-ee5f-40e3-aa1f-ff43b0d29574`", UNIVERSE, None, DOC_IDS) == set()
    assert named_contracts(f"see `{ID_ELSEWHERE}`", UNIVERSE, None, DOC_IDS) == set()
    # Read as a title is: hyphens may go, and a short id is too likely to be a page number or a count.
    assert named_contracts(ID_STAAR.replace("-", ""), UNIVERSE, None, DOC_IDS) == {STAAR}
    assert named_contracts(f"see `{ID_STAAR[:13]}…`", UNIVERSE, None, DOC_IDS) == set(), "a truncated id never resolves"
    assert named_contracts("7 contracts, see p. 7", UNIVERSE, None, DOC_IDS) == set()
    # Titles and filers keep working beside the map, and the map adds to them.
    assert named_contracts(f"Staar Surgical Company; and `{ID_EDIETS}`", UNIVERSE, None, DOC_IDS) == {STAAR, EDIETS}
    assert named_contracts(f"| *{EDIETS}* |", UNIVERSE, None, DOC_IDS) == {EDIETS}
    assert named_contracts("", UNIVERSE, None, DOC_IDS) == set() and named_contracts(by_id, UNIVERSE, None, {}) == set()
    # Negation is not read, for an id as for a title: the rule's stated cost.
    assert named_contracts(
        f"Not included: `{ID_EDIETS}` (the word appears only in warranties)", UNIVERSE, None, DOC_IDS
    ) == {EDIETS}


def test_a_list_scored_with_the_map_counts_an_item_named_by_id_and_without_it_scores_as_before():
    entry = {"kind": "list", "positives": [STAAR, EDIETS, ACME_A], "universe": UNIVERSE}
    text = f"`{ID_STAAR}` and `{ID_EDIETS}` and `{ID_ELSEWHERE}`."
    with_map = score(entry, text, DOC_IDS)
    assert (with_map["named"], with_map["right"], with_map["precision"], with_map["recall"]) == (2, 2, 1.0, 0.667)
    assert with_map["score"] == pytest.approx(0.8)
    assert score(entry, text) == score(entry, text, None) and score(entry, text)["score"] == 0.0
    # A fact is not a list: the map is accepted and changes nothing.
    assert score({"kind": "fact", "all_of": [["Texas"]]}, "Texas law.", DOC_IDS)["score"] == 1.0


def test_the_map_is_read_from_a_responses_citations_at_either_level_and_from_nothing_else():
    from scripts.test_corpora.runner.answer_key import titles_by_doc_id

    citations = [
        {"doc_id": ID_STAAR, "doc_title": STAAR, "pages": "3-4"},
        {"source": {"doc_id": ID_EDIETS, "doc_title": EDIETS}, "score": 0.7},
        {"doc_id": ID_ELSEWHERE},  # no title: nothing to resolve to
        {"source": {"doc_title": ACME_A}},  # no id: nothing an answer could write
        "a bare id from an older response",
    ]
    assert titles_by_doc_id(citations) == {ID_STAAR: STAAR, ID_EDIETS: EDIETS}
    assert titles_by_doc_id(None) == {} and titles_by_doc_id([]) == {}


def test_names_in_questions_and_years_in_keys():
    assert cuad_key.display_name(STAAR) == "Staar Surgical Company Distributor Agreement"
    assert cuad_key.display_name(EDIETS) == "Ediets Com Inc Co-Branding Agreement"
    assert cuad_key._iso("3/15/22") == "2022-03-15" and cuad_key._iso("10/30/00") == "2000-10-30"
    assert cuad_key._iso("8/26/99") == "1999-08-26" and cuad_key._iso("02/04/2020") == "2020-02-04"
    assert cuad_key._iso("[]/[]/2020") is None and cuad_key._iso("perpetual") is None and cuad_key._iso("") is None


def _workdir(tmp_path: Path, texts: dict[str, str], rows: list[dict]) -> Path:
    import csv

    base = tmp_path / "cuad"
    (base / "ingest").mkdir(parents=True)
    (base / "extracted" / "CUAD_v1").mkdir(parents=True)
    for title in texts:
        (base / "ingest" / f"{title}.pdf").write_bytes(b"%PDF-1.4 stub")
    columns = ["Filename", "Governing Law-Answer", "Agreement Date-Answer", "Expiration Date-Answer"]
    columns += [f"{c}-Answer" for c, _ in cuad_key.LIST_CLAUSES]
    with open(base / "extracted" / "CUAD_v1" / "master_clauses.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in columns} for r in rows)
    return tmp_path


def test_a_single_contract_key_must_be_in_that_contracts_own_words(tmp_path, monkeypatch):
    """The annotators also record dates they worked out ("one year from the effective date" as 10/1/02). A model
    answering in the contract's terms would be marked wrong for it, so such a question is not asked."""
    beta = "BetaWidgetsCorp_20010101_10-K_EX-10.1_4_EX-10.1_Supply Agreement"
    texts = {
        STAAR: "governed by the laws of the State of California. Dated as of August 1, 2018.",
        EDIETS: "This Agreement is dated October 30, 2000 and runs for one year from the Effective Date.",
        beta: "The initial term is one year from the Effective Date. Dated as of the 5th day of January, 2001.",
        ACME_A: "governed by the laws of Delaware.",
        ACME_B: "governed by the laws of Delaware.",
    }
    rows = [
        {
            "Filename": f"{STAAR}.pdf",
            "Governing Law-Answer": "California",
            "Agreement Date-Answer": "8/1/18",
            "Insurance-Answer": "Yes",
        },
        {
            "Filename": f"{EDIETS}.pdf",
            "Agreement Date-Answer": "10/30/00",
            "Expiration Date-Answer": "10/30/01",
            "Insurance-Answer": "No",
        },
        {"Filename": f"{beta}.pdf", "Expiration Date-Answer": "1/5/02", "Insurance-Answer": "No"},
        {"Filename": f"{ACME_A}.pdf", "Governing Law-Answer": "Delaware", "Insurance-Answer": "Yes"},
        {"Filename": f"{ACME_B}.pdf", "Governing Law-Answer": "Delaware", "Insurance-Answer": "No"},
    ]
    monkeypatch.setattr(cuad_key, "_text", lambda pdf: texts[pdf.stem])
    questions, key = cuad_key.build(_workdir(tmp_path, texts, rows))
    by_text = {q["text"]: key[q["id"]] for q in questions["ask"]}
    assert questions["research"] == []
    assert [q["id"] for q in questions["ask"]] == [f"cuad-key-{i}" for i in range(1, len(key) + 1)]
    # A filer with two contracts is never asked about by name: "the Acme Holdings Inc agreement" is two.
    assert not [t for t in by_text if "Acme" in t]
    law = by_text["What is the governing law of the Staar Surgical Company Distributor Agreement?"]
    assert law == {"kind": "fact", "all_of": [["California"]], "contract": STAAR}
    # One question per contract: Staar's law was asked, so its date is not.
    assert [t for t in by_text if "Staar" in t] == [
        "What is the governing law of the Staar Surgical Company Distributor Agreement?"
    ]
    assert by_text["What is the agreement date of the Ediets Com Inc Co-Branding Agreement?"]["date"] == "2000-10-30"
    # Beta's expiration is the annotators' arithmetic (a year after 5 January 2001) and is nowhere in the
    # contract, and Ediets' likewise: neither is asked.
    assert not [t for t in by_text if "expire" in t]
    insurance = by_text[
        "Which contracts contain a requirement that a party maintain insurance? List every one you can find."
    ]
    assert insurance["positives"] == sorted([STAAR, ACME_A]) and insurance["universe"] == sorted(texts)
    delaware = by_text["Which contracts are governed by Delaware law? List every one you can find."]
    assert delaware["positives"] == sorted([ACME_A, ACME_B])


def test_the_committed_key_answers_the_committed_questions():
    questions = yaml.safe_load((KEYED / "cuad.yaml").read_text())
    key = json.loads((KEYED / "cuad.key.json").read_text())
    assert [q["id"] for q in questions["ask"]] == list(key) and questions["research"] == []
    kinds = [e["kind"] for e in key.values()]
    assert (kinds.count("fact"), kinds.count("date"), kinds.count("list")) == (8, 6, 6)
    for entry in key.values():
        if entry["kind"] == "list":
            assert 5 <= len(entry["positives"]) <= 25 and set(entry["positives"]) <= set(entry["universe"])
            assert len(entry["universe"]) == 79, "the 80 sampled contracts, less the one CUAD's CSV does not annotate"


def test_the_sweep_loads_the_key_beside_the_questions_and_scores_against_it(tmp_path):
    """questions/keyed/cuad.yaml with cuad.key.json beside it: the key is loaded; the standard set has none."""
    from scripts.test_corpora.runner import sweep

    questions, keys = sweep.load_questions(None)
    assert set(questions) == {"cuad", "enron", "synthetic"} and keys == {"cuad": {}, "enron": {}, "synthetic": {}}
    questions, keys = sweep.load_questions(KEYED)
    assert [q["id"] for q in questions["cuad"]["ask"]] == list(keys["cuad"]) and len(keys["cuad"]) == 20
    assert keys["enron"] == {} and questions["enron"] == sweep.load_questions(None)[0]["enron"], (
        "no override: the standard"
    )
    # A directory with a questions file and no key: questions taken, key {}.
    (tmp_path / "cuad.yaml").write_text(
        yaml.safe_dump({"research": [], "ask": [{"id": "x", "text": "?", "notes": ""}]})
    )
    questions, keys = sweep.load_questions(tmp_path)
    assert questions["cuad"]["ask"][0]["id"] == "x" and keys["cuad"] == {}


def test_the_metrics_cell_is_the_keys_score_or_empty_without_a_key():
    from scripts.test_corpora.runner.state import Status
    from scripts.test_corpora.runner.sweep import key_score_cell

    entry = {"kind": "list", "positives": [STAAR, EDIETS], "universe": UNIVERSE}
    result = {"answer": "Staar Surgical Company has one.", "citations": [{"source": {"doc_title": EDIETS}}]}
    assert key_score_cell(entry, result, Status.DONE) == "0.667", "the citation is a retrieval hit, not a name"
    fact = {"kind": "fact", "all_of": [["Texas"]]}
    assert key_score_cell(fact, {"answer": "Texas law."}, Status.DONE) == "1.000"
    assert key_score_cell(fact, {"answer": "I could not find it."}, Status.DEGRADED) == "0.000", "a give-up is wrong"
    # An outage is not a wrong answer: the judge columns of that row are blank, and so is this one.
    assert key_score_cell(fact, None, Status.ERROR) == "" and key_score_cell(fact, {"answer": "Texas"}, None) == ""
    assert key_score_cell(None, result, Status.DONE) == "" and key_score_cell({}, result, Status.DONE) == ""


def test_the_metrics_cell_resolves_a_document_id_the_answer_wrote_through_the_responses_citations():
    """The phase-4 site of the rule: the response's citations are the map, at either level the app writes them,
    and a citation the answer does not write out is still a retrieval hit, not a name."""
    from scripts.test_corpora.runner.state import Status
    from scripts.test_corpora.runner.sweep import key_score_cell

    entry = {"kind": "list", "positives": [STAAR, EDIETS], "universe": UNIVERSE}
    citations = [
        {"doc_id": ID_STAAR, "doc_title": STAAR},
        {"source": {"doc_id": ID_EDIETS, "doc_title": EDIETS}},
        {"source": {"doc_id": ID_ELSEWHERE, "doc_title": ACME_A}},
    ]
    result = {"answer": f"Two: `{ID_STAAR}` and `{ID_EDIETS}`.", "citations": citations}
    assert key_score_cell(entry, result, Status.DONE) == "1.000"
    assert key_score_cell(entry, {**result, "citations": []}, Status.DONE) == "0.000", "no map, no names"
    three = {**result, "answer": f"`{ID_STAAR}`, `{ID_EDIETS}` and `{ID_ELSEWHERE}`."}
    assert key_score_cell(entry, three, Status.DONE) == "0.800", "the third id names Acme, which is not labelled"


# ── the synthetic key's kinds: counts, amounts, both languages (#779's pairs measured against a key) ──


@pytest.fixture
def _no_corpus_lookup():
    """These entries carry no generation, so the scorer never looks for a corpus; said so, in one place."""
    from scripts.test_corpora.runner import answer_key

    answer_key.bind_corpus_root(None)
    yield
    answer_key.bind_corpus_root(None)


@pytest.mark.parametrize(
    "answer",
    [
        "Initech Software issued 15 invoices in 2025.",
        "**15** invoices.",
        "There are fifteen invoices from Initech.",
        "Initech Software a émis quinze factures à Marbledock en 2025.",
        "15 factures, pour un total de 275 926,50 $.",
        "Fifteen (15) invoices, dated 2025-01-13, 2025-01-20, February 2, 2025 and 12 others.",
        "I count 15: INV-2025-00847 (six times), INV-2025-04417, INV-2025-04872, INV-2025-04817 (three times)...",
        "- 0008_invoice.txt\n- 0012_invoice.txt\n\nTotal: 15 invoices from Initech in 2025, worth $275,926.50 (15% of spend).",
        "Total billed by Initech: $275,926.50\n15 invoices were issued in 2025.",
        "In 2025 - 15 invoices were issued.",
    ],
)
def test_a_count_is_found_as_digits_or_as_a_word_in_either_language(answer, _no_corpus_lookup):
    result = score({"kind": "count", "count": 15}, answer)
    assert result["score"] == 1.0 and 15 in result["numbers"], answer


@pytest.mark.parametrize(
    "answer",
    [
        "Initech Software issued 16 invoices in 2025.",
        "Fourteen invoices.",
        "quatorze factures",
        "1. INV-2025-00847\n2. INV-2025-04417\n" + "".join(f"{i}. INV-2025-0{i}\n" for i in range(3, 16)),
        "The total was $15,192.33 across 2025-02-15 and February 15, 2025; 15% was tax; see 0015_invoice.txt at 9:15.",
        "Initech issued 14 invoices; Globex 20, Cyberdyne 25 and 15 for someone else.",
        "Of the 60 invoices, 15 are Initech's.",
        "",
    ],
)
def test_another_number_is_not_the_count(answer, _no_corpus_lookup):
    """A list of fifteen items ends in "15." and is not a count of 15; neither is a day, a percentage, an amount, a
    time or a file name. The count is the first number stated (review of #812): a wrong answer that mentions the
    right number later is wrong, and so, as the rule's cost, is a right answer that states another number first."""
    assert score({"kind": "count", "count": 15}, answer)["score"] == 0.0, answer


@pytest.mark.parametrize(
    "answer",
    [
        "Q3 2025 – 11 board meetings.",
        "During Q3 2025 (Jul–Sept), the board held 11 meetings.",
        "Between July and Sept., the board held 11 meetings.",
        "Du juin au septembre 2025, le conseil a tenu 11 réunions.",
        "Meetings: $0 cost\n11 in Q3.",
    ],
)
def test_a_year_before_a_dash_a_month_beside_another_or_an_english_sept_is_not_a_count(answer, _no_corpus_lookup):
    """Second review of #812: a year and a dash read as a range, "Sept" as the French seven, and a money amount
    swallowed the digits on the next line."""
    assert score({"kind": "count", "count": 11}, answer) == {"score": 1.0, "numbers": [11]}, answer


def test_the_french_seven_is_read_as_a_word_but_not_with_a_full_stop(_no_corpus_lookup):
    from scripts.test_corpora.runner.answer_key import counts_in

    assert counts_in("sept factures, puis sept réunions") == [7]
    assert counts_in("Il y en a sept.") == [], "the cost of reading 'Sept.' as September, stated in the module"


def test_the_first_number_is_the_count_whoever_it_belongs_to(_no_corpus_lookup):
    """Two campaigns with seven briefs each: the rule cannot tell them apart, and says so."""
    seven = {"kind": "count", "count": 7}
    assert score(seven, "Harbour Lights has 6 briefs and Skylight has 7.")["score"] == 0.0
    assert score(seven, "Skylight has 7 briefs and Harbour Lights 6.")["score"] == 1.0, "the cost, pinned"
    assert score(seven, "Five briefs for Harbour Lights, the last dated Sept 2025.") == {"score": 0.0, "numbers": [5]}
    assert score(seven, "Sept briefs pour Harbour Lights, le dernier daté de sept. 2025.")["score"] == 1.0


def test_what_counts_in_reads_and_what_it_strips(_no_corpus_lookup):
    from scripts.test_corpora.runner.answer_key import counts_in

    assert counts_in("Twenty-five invoices; thirty one documents; 1,234 pages; vingt-cinq réunions; dix-sept; 25") == [
        25,
        31,
        1234,
        17,
    ], "a compound word is one number, not its parts; in order of appearance, each once"
    assert counts_in("In 2025 there were 11 board meetings, the last on November 26, 2025 (26 Nov 2025).") == [11]
    assert counts_in("The market 15 and decided 5: 15 factures pour 275 926,50 $.") == [15, 5], (
        "a month is a whole word"
    )
    assert counts_in("Oct 29-30, 2025; invoice CDI-2025-08841; ref 【141b4469-c5c0】; 24x5; Sept 2025; sept. 15") == []
    assert counts_in("une réunion, one of them, un seul; vingt-et-un, twenty-one") == [21], (
        "one, un and une are an article and a pronoun before they are a number; inside a compound they count"
    )
    assert counts_in("Paid $1,000\n150 invoices were issued.") == [150], (
        "a line break is not a thousands separator, whatever follows it (second review of #812)"
    )
    assert counts_in("15.") == [15] and counts_in("15.\n") == [15] and counts_in("1. INV-2025-00847\n2. B") == [], (
        "a list marker has text after it on its line; a bare number with a full stop is the answer"
    )


@pytest.mark.parametrize(
    "answer",
    [
        "The total is $364,107.38.",
        "USD 364,107.38 across 25 invoices.",
        "**364 107,38 $** au total.",
        "364107.38",
        "Le total s'élève à 364 107,38 dollars.",
        "approximately $364,107 in all",
        "Total: 364.107,38 €",
    ],
)
def test_an_amount_is_found_however_its_digits_are_grouped(answer, _no_corpus_lookup):
    assert score({"kind": "amount", "amount": 364107.38}, answer)["score"] == 1.0, answer


@pytest.mark.parametrize(
    "answer", ["$364,107.39", "$364,100", "$364,108 (rounded up)", "$3,641,073.80", "36,410.74", "about $364k", ""]
)
def test_another_amount_is_not_it(answer, _no_corpus_lookup):
    """To the cent, or the whole dollars rounded or truncated; 364,108 is neither, and 364k is not a number."""
    assert score({"kind": "amount", "amount": 364107.38}, answer)["score"] == 0.0, answer


def test_a_whole_dollar_amount_accepts_cents_of_zero(_no_corpus_lookup):
    assert score({"kind": "amount", "amount": 12500}, "a flat monthly fee of USD $12,500.00")["score"] == 1.0
    assert score({"kind": "amount", "amount": 12500}, "12 500 $ par mois")["score"] == 1.0
    assert score({"kind": "amount", "amount": 12500}, "$12,500.50")["score"] == 0.0
    assert score({"kind": "amount", "amount": 2050.25}, "$2,050.25 and $2,050")["score"] == 1.0


def test_amounts_in_reads_each_grouping_once():
    from decimal import Decimal

    from scripts.test_corpora.runner.answer_key import amounts_in

    found = amounts_in("$364,107.38; 364 107,38 $; 2,580,000; 12.500; 12,500.00; 1,5; 2025-11-04; 1.2.3")
    assert found == {Decimal(v) for v in ("364107.38", "2580000", "12500", "12500.00", "1.5", "2025", "11", "04")}
    assert Decimal("12500") in found and Decimal("12500.00") in found, "one value, two spellings"


def test_the_absent_opening_rule_covers_counts_and_amounts_in_both_languages(_no_corpus_lookup):
    count, amount = {"kind": "count", "count": 15}, {"kind": "amount", "amount": 364107.38}
    assert score(count, "I could not find any invoices from Initech; the corpus lists 15 documents.") == {
        "score": 0.0,
        "absent": "could not find",
    }
    assert score(count, "Je n’ai pas trouvé de factures d'Initech, mais 15 documents la mentionnent.") == {
        "score": 0.0,
        "absent": "je n'ai pas trouvé",
    }, "the typographic apostrophe is the plain one"
    assert score(amount, "Aucune information sur le total; les factures visibles font 364 107,38 $.")["score"] == 0.0
    assert score(amount, "Le total n'est pas indiqué. Les 25 factures font 364 107,38 $.")["score"] == 0.0
    # Said later, it is a caveat, not the answer: the rule reads the opening sentence only.
    assert score(amount, "364 107,38 $. Je n'ai pas trouvé de facture pour décembre.")["score"] == 1.0
    assert score(count, "15 factures. Aucune information sur leur paiement.")["score"] == 1.0
    assert score(count, "<think>Je n'ai pas trouvé.</think>\n15 factures.")["score"] == 1.0


@pytest.mark.parametrize(
    "text",
    [
        "le 1er décembre 2025",
        "effective Monday, le 1er decembre 2025.",
        "December 1, 2025",
        "1 déc. 2025",
        "01/12/2025",
        "1.12.2025",
        "01-12-2025",
        "12/1/2025",
        "12-01-2025",
        "2025-12-01",
    ],
)
def test_a_canadian_date_is_found_in_french_in_english_and_in_either_numeric_convention(text, _no_corpus_lookup):
    assert score({"kind": "date", "date": "2025-12-01", "day_first": True}, text)["score"] == 1.0


@pytest.mark.parametrize(
    "text", ["11/12/2025", "December 10, 2025", "le 2 décembre 2025", "1er novembre 2025", "December 1.", "le 1er déc."]
)
def test_another_canadian_date_is_not_it(text, _no_corpus_lookup):
    """Without `year_optional`, a month and day are not the date."""
    assert score({"kind": "date", "date": "2025-12-01", "day_first": True}, text)["score"] == 0.0


@pytest.mark.parametrize(
    "text", ["December 1.", "The board met on December 1st.", "Le 1er décembre.", "1 December", "1er déc.", "Dec. 01"]
)
def test_a_month_and_day_are_the_date_when_the_question_named_the_year(text, _no_corpus_lookup):
    entry = {"kind": "date", "date": "2025-12-01", "day_first": True, "year_optional": True}
    assert score(entry, text)["score"] == 1.0, text
    # Followed by another year, it is another date; numerals without a year are two days and are not read.
    assert score(entry, "December 1, 2024")["score"] == 0.0 and score(entry, "on 1/12")["score"] == 0.0


def test_day_first_numerals_are_read_only_when_the_entry_says_so(_no_corpus_lookup):
    """CUAD's entries do not say so, and 3/4/2022 stays one day for them (review of #697)."""
    assert score({"kind": "date", "date": "2025-12-01"}, "01/12/2025")["score"] == 0.0
    assert score({"kind": "date", "date": "2025-12-01"}, "le 1er décembre 2025")["score"] == 1.0, (
        "words are unambiguous"
    )
    assert "1decembre2025" in date_spellings("2025-12-01") and "01/12/2025" not in date_spellings("2025-12-01")
    assert "01/12/2025" in date_spellings("2025-12-01", day_first=True)


def test_a_fact_is_read_with_its_accents_folded(_no_corpus_lookup):
    entry = {"kind": "fact", "all_of": [["Madeleine Côté-Renaud"]]}
    assert score(entry, "The letter is signed by Madeleine Cote-Renaud.")["score"] == 1.0
    assert score(entry, "Signée par **Madeleine Côté‑Renaud**, Directrice.")["score"] == 1.0
    assert score(entry, "Madeleine Tremblay-Renaud")["score"] == 0.0
    assert score({"kind": "fact", "all_of": [["Globex Supplies", "Globex"]]}, "C'est Globex.")["score"] == 1.0


def test_a_list_item_is_also_named_by_an_alias_between_word_boundaries(_no_corpus_lookup):
    universe = ["0001_invoice", "0012_invoice", "0044_invoice", "0053_invoice"]
    aliases = {
        "0001_invoice": ["CDI-2025-08841", "10725.41", "10725"],
        "0012_invoice": ["17195.51", "17195"],
        "0044_invoice": ["19062.83", "19062"],
        "0053_invoice": ["12117.22", "12117"],
    }
    table = "| CDI-2025-08841 | Nov 4 | $10,725.41 |\n| INV-2025-00847 | Feb 2 | $17,195.51 |\n| 0044_invoice.txt | $19,062 |"
    assert named_contracts(table, universe, aliases) == {"0001_invoice", "0012_invoice", "0044_invoice"}
    assert named_contracts("117195 and 1211722 and 12 117 22", universe, aliases) == {"0053_invoice"}, (
        "a longer number is not the alias; a grouped one is"
    )
    # A numeric alias is a number the answer states, to the value: a larger amount ending in the same digits is not
    # it, and neither is the same whole-dollar figure with other cents (review of #812: 0057's tax line named 0033).
    assert (
        named_contracts("tax $1,200.80; subtotal $16,215.55", ["0033", "0027"], {"0033": ["20080"], "0027": ["16215"]})
        == set()
    )
    assert named_contracts("$20,080.00 and 16 215 $", ["0033", "0027"], {"0033": ["20080"], "0027": ["16215"]}) == {
        "0033",
        "0027",
    }
    assert named_contracts("$10 725,41", universe, aliases) == {"0001_invoice"}
    assert named_contracts("", universe, aliases) == set() and named_contracts("nothing", universe) == set()
    entry = {"kind": "list", "positives": ["0001_invoice", "0012_invoice"], "universe": universe, "aliases": aliases}
    assert score(entry, table)["score"] == pytest.approx(0.8), "two right of three named, of two labelled"
    assert score({**entry, "aliases": None}, table)["score"] == 0.0, "without aliases, only a stem names an invoice"


def test_an_item_under_a_heading_or_lead_in_that_marks_exclusion_is_not_named():
    """The four shapes the cloud subjects wrote (bench-20261014-2328-cuad-fable keys 16, 18 and 20;
    bench-20261015-0011-cuad-astra key 16), with the test universe's contracts in place of theirs."""
    from scripts.test_corpora.runner.answer_key import without_excluded_sections

    # Fable key-16: a `##` heading whose section is one bold item.
    false_positive = (
        f"## Explicit, mutual non-disparagement clauses (1)\n\n**1. Staar – Distributor** (doc_id `{ID_STAAR}`, p. 3)\n\n"
        f"## False positive (excluded)\n\n**eDiets / Women.com – Co-Branding Agreement** (doc_id `{ID_EDIETS}`, "
        "pp. 13–14) — the word appears only in content warranties.\n"
    )
    assert named_contracts(false_positive, UNIVERSE, None, DOC_IDS) == {STAAR}
    # Fable key-18: bulleted items under a hedged heading, then a second excluded section.
    borderline = (
        f"## Contracts with an operative right of first refusal\n\n- **Staar** (`{ID_STAAR}`), §4\n\n"
        "## Mentions / borderline (included for completeness, not operative ROFRs in the document itself)\n\n"
        f"- **Women.com** (`{ID_EDIETS}`), §9.1(d) — a cross-reference to the separate Franchise Agreement.\n\n"
        f'## Reviewed and excluded\n"First right" hits that are enforcement priorities: {EDIETS}.\n'
    )
    assert named_contracts(borderline, UNIVERSE, None, DOC_IDS) == {STAAR}
    # Fable key-20: a near-miss heading, and a heading after it that is kept (the drop ends there).
    near_miss = (
        f"## Contracts governed by Delaware law — 1 found\n\n- **Staar** (`{ID_STAAR}`)\n\n"
        f"## Near-miss worth flagging (not Delaware-governed)\n\n- **Women.com** (`{ID_EDIETS}`): New York law.\n\n"
        f"## Method / caveats\n\nThe {EDIETS} hit above was read in full.\n"
    )
    assert named_contracts(near_miss, UNIVERSE, None, DOC_IDS) == {STAAR, EDIETS}, "the caveats section is kept"
    # astra key-16: a bold lead-in with the item on its own line, after a table that is kept.
    not_counted = (
        "### Contracts containing non-disparagement restrictions\n\n| Contract | Provision |\n|---|---|\n"
        f"| **[STAAR Surgical Company — Distributor Agreement](doc_id:{ID_STAAR})** | §6.7 |\n\n"
        f"**One additional keyword match, not counted above:** [eDiets.com](doc_id:{ID_EDIETS}), content warranties.\n\n"
        "I searched the corpus for the stem **“disparag”**.\n"
    )
    assert named_contracts(not_counted, UNIVERSE, None, DOC_IDS) == {STAAR}
    # Mid-line bold is not a lead-in, so the excluded lead-in's drop runs over the trailing prose to the end...
    assert "searched the corpus" not in without_excluded_sections(not_counted)
    # ...and ends at the next heading or lead-in.
    assert "searched the corpus" in without_excluded_sections(
        not_counted.replace("I searched", "### Method\nI searched")
    )
    assert "searched the corpus" in without_excluded_sections(
        not_counted.replace("I searched", "**Method:** I searched")
    )
    # A heading that does not mark exclusion keeps its section, with the words the patterns must not over-read.
    kept = f"## Contracts with the clause\n\n- {STAAR}\n\n## Exclusive license grants (2)\n\n- `{ID_EDIETS}`\n"
    assert named_contracts(kept, UNIVERSE, None, DOC_IDS) == {STAAR, EDIETS}
    assert named_contracts(
        f"## No. 2: {STAAR}\n**Perpetual only (not described as irrevocable)**\n`{ID_EDIETS}`", UNIVERSE, None, DOC_IDS
    ) == {STAAR, EDIETS}
    # A bare "no" is not a marker: on the real data it dropped only a section holding a labelled contract, and
    # these headings are not rejects.
    no_but_kept = (
        f"Found two.\n\n## Contracts governed by Delaware law — no exceptions\n`{ID_STAAR}`\n\n"
        f"## Disparagement as a termination trigger only — no affirmative covenant (1)\n`{ID_EDIETS}`\n\n"
        f"## Matches (no duplicates)\n{ACME_A}\n"
    )
    assert named_contracts(no_but_kept, UNIVERSE, None, DOC_IDS) == {STAAR, EDIETS, ACME_A}


def test_a_dropped_heading_takes_its_sub_headings_and_lead_ins_and_the_first_section_is_never_dropped():
    from scripts.test_corpora.runner.answer_key import without_excluded_sections

    text = (
        f"Found one.\n\n## Matches\n\n### 1. Staar\n`{ID_STAAR}`\n\n## Reviewed and excluded\n\n### 1. Women.com\n"
        f"`{ID_EDIETS}`\n\n**Also:** {ACME_A}\n\n## Summary\n\nOne contract: Staar Surgical Company.\n"
    )
    assert named_contracts(text, UNIVERSE, None, DOC_IDS) == {STAAR}
    assert without_excluded_sections(text).endswith("## Summary\n\nOne contract: Staar Surgical Company.")
    # A lead-in's section ends at the next lead-in or heading; a heading's does not end at a lead-in.
    assert named_contracts(f"**Not a match:** `{ID_EDIETS}`\n**Match:** `{ID_STAAR}`\n", UNIVERSE, None, DOC_IDS) == {
        EDIETS,
        STAAR,
    }, "the first section is never dropped, even when it opens with a marked lead-in"
    assert named_contracts(
        f"Two.\n**Not a match:** `{ID_EDIETS}`\n**Match:** `{ID_STAAR}`\n", UNIVERSE, None, DOC_IDS
    ) == {STAAR}
    assert named_contracts(f"Two.\n## Excluded\n**Match:** `{ID_STAAR}`\n", UNIVERSE, None, DOC_IDS) == set()
    # Negation in prose is still not read: the rule reads headings and lead-ins only.
    assert named_contracts(f"Not included: `{ID_EDIETS}` (warranties only)", UNIVERSE, None, DOC_IDS) == {EDIETS}
    # The shape it cannot read, stated as a cost in the module docstring: an excluded lead-in followed by `###`
    # items excludes only itself, because a heading ends a lead-in's section.
    unread = f"Found one: `{ID_STAAR}`.\n\n**Not counted:**\n### 1. Women.com\n**Doc ID:** `{ID_EDIETS}`\n"
    assert named_contracts(unread, UNIVERSE, None, DOC_IDS) == {STAAR, EDIETS}
    assert without_excluded_sections("") == "" and without_excluded_sections("plain answer") == "plain answer"
