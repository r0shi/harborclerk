"""The answer key scores an answer against human labels. Its matching is crude on purpose; these pin exactly how
crude, because a scorer that is wrong punishes the model that was right."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.corpora import cuad_key
from scripts.test_corpora.runner.answer_key import date_spellings, named_contracts, score

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


def test_a_contract_is_named_by_citation_by_its_whole_title_or_by_a_filer_with_one_contract():
    assert named_contracts("nothing here", [EDIETS], UNIVERSE) == {EDIETS}
    typographic = EDIETS.replace("-", "‑")  # how a model renders it; the first bake-off met exactly this
    assert named_contracts(f"| 1 | *{typographic}* | p. 17 |", [], UNIVERSE) == {EDIETS}
    assert named_contracts("the Staar Surgical Company distributor agreement", [], UNIVERSE) == {STAAR}
    # Two contracts from one filer: its name alone says neither.
    assert named_contracts("Acme Holdings Inc has such a clause", [], UNIVERSE) == set()
    assert named_contracts("Acme Holdings Inc", [ACME_B], UNIVERSE) == {ACME_B}
    # A filer name under eight characters is too likely to be a word.
    assert named_contracts("the ABC agreement", [], UNIVERSE) == set()
    assert named_contracts("", [], UNIVERSE) == set() and named_contracts("x", [None, ""], UNIVERSE) == set()


def test_a_list_is_scored_by_f1_so_that_recall_shows():
    entry = {"kind": "list", "positives": [STAAR, EDIETS, ACME_A, ACME_B], "universe": UNIVERSE}
    one = score(entry, "Only one: Staar Surgical Company.", [])
    assert (one["named"], one["right"], one["precision"], one["recall"]) == (1, 1, 1.0, 0.25)
    assert one["score"] == pytest.approx(0.4), "right as far as it goes, and a quarter of the answer"
    everything = score(entry, "", UNIVERSE)
    assert (everything["precision"], everything["recall"]) == (0.8, 1.0) and everything["score"] == pytest.approx(8 / 9)
    assert score(entry, "No contract in the corpus has such a clause.", [])["score"] == 0.0
    assert score({**entry, "positives": []}, "Staar Surgical Company", [])["score"] == 0.0
    with pytest.raises(ValueError, match="unknown key entry kind"):
        score({"kind": "essay"}, "x")


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
