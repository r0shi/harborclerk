"""The synthetic answer key is written from the corpus's fact sidecars by rules. These pin the rules, that the same
sidecars give the same key, that the committed key is what the committed sidecars give, and that the scorer refuses
the key against any other generation of the corpus."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.corpora import synthetic_key
from scripts.test_corpora.corpora.synthetic_key import (
    Corpus,
    SnapshotIncomplete,
    build,
    verify_aggregates,
    write_snapshot,
)
from scripts.test_corpora.runner import answer_key, sweep
from scripts.test_corpora.runner.answer_key import CorpusMismatch, generation_id, score
from scripts.test_corpora.runner.quality import find_unfilled_placeholder

KEYED = synthetic_key.OUT_DIR
SNAPSHOT = synthetic_key.SNAPSHOT_DIR
GEN = "generation"


@pytest.fixture(autouse=True)
def _unbound():
    """No test scores against whatever corpus this machine has under the default workdir."""
    answer_key.bind_corpus_root(None)
    yield
    answer_key.bind_corpus_root(None)


def _ids(questions: dict) -> list[str]:
    """Question ids as the sweep asks them: a cross-language pair is `<id>__en` and `<id>__fr`."""
    return sweep._question_ids(questions)


def _committed() -> tuple[dict, dict]:
    return (
        yaml.safe_load((KEYED / "synthetic.yaml").read_text()),
        json.loads((KEYED / "synthetic.key.json").read_text()),
    )


# ── the committed key ──


def test_the_committed_key_is_a_fresh_extraction_from_the_committed_sidecars():
    """A stale commit fails here: the snapshot holds every sidecar of the generation and the texts the build checks
    keys against, so the build reproduces the committed files byte for byte in meaning."""
    questions, key = build(Corpus.load(SNAPSHOT))
    committed_questions, committed_key = _committed()
    assert questions == committed_questions, (
        "questions/keyed/synthetic.yaml is not what the snapshot gives; regenerate with "
        "`python -m scripts.test_corpora.corpora.synthetic_key --corpus-dir tests/fixtures/synthetic_corpus --redo`"
    )
    assert key == committed_key, "questions/keyed/synthetic.key.json is not what the snapshot gives; regenerate it"


def test_every_question_has_one_key_and_every_key_one_question():
    questions, key = _committed()
    header = key["_generation"]
    entries = {qid: e for qid, e in key.items() if qid != "_generation"}
    assert questions["research"] == [] and _ids(questions) == list(entries)
    assert header["corpus"] == "synthetic" and header["questions"] == len(questions["ask"]) == 20
    assert header["documents"] == header["sidecars"] == 280 and header["unusable_sidecars"] == 17
    assert header[GEN] == generation_id(SNAPSHOT / "facts") and len(header[GEN]) == answer_key.GENERATION_DIGITS
    assert all(e[GEN] == header[GEN] for e in entries.values()), "every entry names the generation it was counted over"
    kinds = [e["kind"] for e in entries.values()]
    assert {k: kinds.count(k) for k in sorted(set(kinds))} == {"amount": 4, "count": 4, "date": 5, "fact": 5, "list": 5}
    pairs = [q for q in questions["ask"] if q.get("cross_language")]
    assert [q["id"] for q in pairs] == ["synthetic-key-4", "synthetic-key-11", "synthetic-key-19"]
    for q in pairs:
        assert [v["lang"] for v in q["variants"]] == ["en", "fr"]
        assert entries[f"{q['id']}__en"] == entries[f"{q['id']}__fr"], "a pair shares one key"
    for q in questions["ask"]:
        assert q["notes"], f"{q['id']} has no notes line saying where its key comes from"
        for text in [v["text"] for v in q["variants"]] if q.get("cross_language") else [q["text"]]:
            assert find_unfilled_placeholder(text) is None
    sidecars = {p.stem for p in (SNAPSHOT / "facts").glob("*.json")}
    for qid, e in entries.items():
        if e["kind"] in ("fact", "date") or "document" in e:
            assert e["document"] in sidecars, qid
        else:
            assert set(e["documents"]) <= sidecars and e["field"], qid
        if e["kind"] == "list":
            assert set(e["positives"]) <= set(e["universe"]) and 1 <= len(e["positives"]) < len(e["universe"])
            for stem, aliases in e.get("aliases", {}).items():
                assert stem in e["universe"]
                others = {a for s, al in e["aliases"].items() if s != stem for a in al}
                assert not set(aliases) & others, f"{qid}: an alias names two invoices"
        if e["kind"] == "date":
            assert e["day_first"] is True, "a Canadian corpus is read in both numeric conventions"


def test_the_committed_single_document_keys_are_in_their_documents_own_words(tmp_path):
    """What the build checked, checked again against the committed files: the key is in the text the model reads."""
    _bind_snapshot(tmp_path)
    corpus = Corpus.load(SNAPSHOT)
    _, key = _committed()
    checked = 0
    for qid, e in key.items():
        if qid != "_generation" and "document" in e:
            assert score(e, corpus.text(e["document"]))["score"] == 1.0, qid
            checked += 1
    assert checked == 12, "eleven documents, one of them asked in two languages"


def test_the_sweep_loads_the_synthetic_key_beside_the_questions():
    questions, keys = sweep.load_questions(KEYED)
    assert set(_ids(questions["synthetic"])) == set(keys["synthetic"]) - {"_generation"}
    assert keys["synthetic"]["synthetic-key-4__fr"]["kind"] == "count"


def _bind_snapshot(tmp_path: Path) -> None:
    """Bind the scorer to a workdir whose synthetic corpus is the snapshot."""
    os.symlink(SNAPSHOT, tmp_path / "synthetic")
    answer_key.bind_corpus_root(tmp_path)


# ── the generation check ──


def test_scoring_refuses_a_key_from_another_generation(tmp_path):
    _, key = _committed()
    entry = key["synthetic-key-2"]
    answer_key.bind_corpus_root(tmp_path / "nowhere")
    with pytest.raises(CorpusMismatch, match="no synthetic corpus"):
        score(entry, "Globex Supplies")
    other = tmp_path / "other" / "synthetic" / "facts"
    other.mkdir(parents=True)
    (other / "0001_invoice.json").write_text('{"vendor": "Someone Else"}')
    answer_key.bind_corpus_root(tmp_path / "other")
    with pytest.raises(
        CorpusMismatch, match=f"for synthetic generation {entry[GEN]}; the corpus under .* is generation"
    ):
        score(entry, "Globex Supplies")
    # The same key against the corpus it was written for scores; an entry without a generation never looks.
    _bind_snapshot(tmp_path)
    assert score(entry, "Globex Supplies")["score"] == 1.0
    answer_key.bind_corpus_root(tmp_path / "nowhere")
    assert score({"kind": "fact", "all_of": [["Globex"]]}, "Globex Supplies")["score"] == 1.0


def test_the_corpus_root_is_the_bound_one_then_the_environment_then_the_command_line_then_the_default(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(answer_key.WORKDIR_ENV, raising=False)
    monkeypatch.setattr("sys.argv", ["sweep", "--run-id", "r", "--workdir", str(tmp_path / "argv"), "--phases", "4"])
    assert answer_key.corpus_root() == tmp_path / "argv", "the sweep's own flag, read without a hook"
    monkeypatch.setenv(answer_key.WORKDIR_ENV, str(tmp_path / "env"))
    assert answer_key.corpus_root() == tmp_path / "env"
    answer_key.bind_corpus_root(tmp_path / "bound")
    assert answer_key.corpus_root() == tmp_path / "bound"
    answer_key.bind_corpus_root(None)
    monkeypatch.delenv(answer_key.WORKDIR_ENV)
    monkeypatch.setattr("sys.argv", ["pytest", "-q", "--workdir"])  # a dangling flag is not ours to reject
    from scripts.test_corpora import conftest as cfg

    assert answer_key.corpus_root() == cfg.WORKDIR_DEFAULT


def test_the_generation_is_the_sidecars_bytes_wherever_they_sit(tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(SNAPSHOT / "facts", copy)
    assert generation_id(copy) == generation_id(SNAPSHOT / "facts")
    (copy / "0001_invoice.json").write_text((copy / "0001_invoice.json").read_text() + "\n")
    assert generation_id(copy) != generation_id(SNAPSHOT / "facts"), "a byte is a different generation"
    with pytest.raises(FileNotFoundError):
        generation_id(tmp_path / "empty")


# ── the rules, on a corpus small enough to read ──


def _write(root: Path, sidecars: dict[str, dict], texts: dict[str, str], pdfs: set[str] = frozenset()) -> Corpus:
    (root / "facts").mkdir(parents=True)
    (root / "ingest").mkdir()
    for stem, facts in sidecars.items():
        (root / "facts" / f"{stem}.json").write_text(json.dumps(facts, indent=2, ensure_ascii=False))
        if stem in pdfs:
            (root / "ingest" / f"{stem}.pdf").write_bytes(b"%PDF-1.4 stub")
        else:
            (root / "ingest" / f"{stem}.txt").write_text(texts.get(stem, f"{stem}\n"))
    return Corpus.load(root)


def _invoice(vendor: str, number: str, date: str, total: float) -> dict:
    return {"vendor": vendor, "invoice_number": number, "date": date, "total_usd": total, "line_items": []}


def _invoice_text(vendor: str, number: str, date_text: str, total_text: str) -> str:
    return f"{vendor.upper()}\nInvoice Number: {number}\nInvoice Date: {date_text}\nTOTAL DUE: {total_text}\n"


SIDECARS: dict[str, dict] = {
    "0001_invoice": _invoice("Alpha Supplies", "A-1", "2025-03-05", 1000.5),
    "0002_invoice": _invoice("Alpha Supplies", "A-2", "2025-03-20", 2000.0),
    "0003_invoice": _invoice("Alpha Supplies", "A-2", "2025-04-01", 2000.75),
    "0004_invoice": _invoice("Beta Software", "B-1", "2025-02-02", 500.25),
    "0005_invoice": _invoice("Beta Software", "B-2", "2025-02-10", 750.0),
    "0006_invoice": _invoice("Beta Software", "B-3", "2025-02-15", 800.0),
    "0007_vendor_contract": {
        "vendor": "Alpha Supplies",
        "term_months": 12,
        "monthly_fee_usd": 4500.0,
        "governing_law": "State of New York",
        "signatures": {},
    },
    "0008_vendor_contract": {
        "vendor": "Beta Software",
        "term_months": 12,
        "monthly_fee_usd": 8500.0,
        "governing_law": "State of Delaware",
        "signatures": {},
    },
    "0009_vendor_contract": {
        "vendor": "Beta Software",
        "term_months": "12",
        "monthly_fee_usd": 8000,
        "governing_law": "State of Delaware",
        "signatures": {},
    },
    "0010_vendor_contract": {
        "vendor": "Beta Software",
        "term_months": 24,
        "monthly_fee_usd": 9000,
        "governing_law": "Province of Ontario",
        "signatures": {},
    },
    "0011_vendor_contract": {"_error": "no JSON in response for vendor_contract: ```json"},
    "0012_onboarding_letter": {
        "employee_name": "New Team Member",
        "role": "Analyst",
        "start_date": "2025-05-12",
        "languages_used": [],
        "signing_manager": "Sophie Trépanier",
    },
    "0013_onboarding_letter": {
        "employee_name": "Élise Fontaine",
        "role": "Coordinator",
        "start_date": "2025-12-01",
        "languages_used": [],
        "signing_manager": "Rémi Thibodeau",
    },
    "0014_onboarding_letter": {
        "employee_name": "Alex Thibodeau",
        "role": "Analyst",
        "start_date": "2025-09-29",
        "languages_used": [],
        "signing_manager": {"name": "Madeleine Côté-Renaud", "title": "Directrice"},
    },
    "0015_onboarding_letter": {
        "employee_name": "Jean Roy",
        "role": "Analyst",
        "start_date": "2025-06-02",
        "languages_used": [],
        "signing_manager": "Thomas Archibald",
    },
    "0016_onboarding_letter": {
        "employee_name": "Jean Roy",
        "role": "Analyst",
        "start_date": "2025-06-09",
        "languages_used": [],
        "signing_manager": "Thomas Archibald",
    },
    "0017_policy_doc": {
        "policy_name": "Information Security & HR Policy",
        "version": "v2.1",
        "effective_date": "2025-08-12",
        "owner": "Human Resources & Compliance Department, Marbledock & Associates",
    },
    "0018_policy_doc": {
        "policy_name": "HR and Information Security Policy",
        "version": "2.1.0",
        "effective_date": "2025-08-13",
        "owner": "Human Resources",
    },
    "0019_policy_doc": {
        "policy_name": "Travel Policy",
        "version": "3.0",
        "effective_date": "2025-10-28",
        "owner": "Finance Department",
    },
    "0020_policy_doc": {
        "policy_name": "Travel Policy",
        "version": "3.1",
        "effective_date": "2025-11-02",
        "owner": "Finance Department",
    },
    "0021_internal_memo": {
        "from": "Helena Voss, Chief Operations Officer",
        "to": "All Staff",
        "subject": "Remote Work Update",
        "lang": "en",
    },
    "0022_internal_memo": {
        "from": "Eleanor Voss, Managing Partner",
        "to": "All Staff",
        "subject": "Remote work update",
        "lang": "en",
    },
    "0023_internal_memo": {
        "from": "Office of the Managing Partner, Marbledock & Associates",
        "to": "All Staff",
        "subject": "Parking",
        "lang": "en",
    },
    "0024_board_minutes": {"date": "2025-01-12", "attendees": [], "decisions": [], "lang": "en"},
    "0025_board_minutes": {"date": "2025-03-02", "attendees": [], "decisions": [], "lang": "en"},
    "0026_board_minutes": {"date": "2025-03-20", "attendees": [], "decisions": [], "lang": "en"},
    "0027_board_minutes": {"date": "2025-07-01", "attendees": [], "decisions": [], "lang": "en"},
    "0028_board_minutes": {"date": "2025-08-01", "attendees": [], "decisions": [], "lang": "en"},
    "0029_board_minutes": {"date": "2025-09-09", "attendees": [], "decisions": [], "lang": "en"},
    "0030_board_minutes": {"date": "2025-09-20", "attendees": [], "decisions": [], "lang": "en"},
    "0031_marketing_brief": {
        "campaign_name": "Skylight",
        "target": "Northwind Partners",
        "budget_usd": 250000,
        "owner": "M",
    },
    "0032_marketing_brief": {
        "campaign_name": "Skylight",
        "target": "Polestar Industries",
        "budget_usd": 480000,
        "owner": "M",
    },
    "0033_marketing_brief": {
        "campaign_name": "Harbour Lights",
        "target": "Acme Corp",
        "budget_usd": 250000,
        "owner": "M",
    },
    "0034_marketing_brief": {
        "campaign_name": "Northstar",
        "target": "Northwind Partners",
        "budget_usd": 250000,
        "owner": "M",
    },
    "0035_marketing_brief": {
        "campaign_name": "Northstar",
        "target": "Polestar Industries",
        "budget_usd": 850000,
        "owner": "M",
    },
    "0036_marketing_brief": {
        "campaign_name": "Northstar",
        "target": "Northwind Partners",
        "budget_usd": 250000,
        "owner": "M",
    },
}
TEXTS: dict[str, str] = {
    "0001_invoice": _invoice_text("Alpha Supplies", "A-1", "March 5, 2025", "$1,000.50"),
    "0002_invoice": _invoice_text("Alpha Supplies", "A-2", "March 20, 2025", "$2,000.00"),
    "0003_invoice": _invoice_text("Alpha Supplies", "A-2", "April 1, 2025", "$2,000.75"),
    "0005_invoice": "INVOICE\nInvoice Number: B-2\nInvoice Date: February 10, 2025\nTOTAL DUE: $750.00\n",  # no vendor
    "0006_invoice": _invoice_text("Beta Software", "B-3", "February 15, 2025", "$800.00"),
    "0007_vendor_contract": "twelve (12) months ... a flat monthly fee of USD $4,500.00 ... laws of the State of New York",
    "0010_vendor_contract": "twenty-four (24) months ... USD $9,000.00 ... governed by the laws of the Province of Ontario",
    "0013_onboarding_letter": "Chère Élise Fontaine, ... effective Monday, le 1er décembre 2025. ... Rémi Thibodeau",
    "0014_onboarding_letter": "Dear Alex Thibodeau, ... le 29 septembre 2025 ... Madeleine Côté-Renaud, Directrice",
    "0019_policy_doc": "Policy Name: Travel Policy\nVersion: 3.0\nEffective Date: October 28, 2025\nPolicy Owner: Finance Department\n",
    "0020_policy_doc": "Policy Name: Travel Policy\nVersion: 3.1\nEffective Date: November 2, 2025\nPolicy Owner: Finance Department\n",
    "0023_internal_memo": "INTERNAL MEMORANDUM\nFROM: Office of the Managing Partner\nSUBJECT: Parking\n",
    "0024_board_minutes": "BOARD MEETING MINUTES\nDate: January 12, 2025\n",
    "0027_board_minutes": "BOARD MEETING MINUTES\nDate: 2025-07-01\n",
}
PDFS = {"0004_invoice", "0028_board_minutes"}


@pytest.fixture
def small(tmp_path) -> Corpus:
    return _write(tmp_path / "synthetic", SIDECARS, TEXTS, PDFS)


def test_the_rules_on_a_small_corpus(small):
    questions, key = build(small)
    by_text = {}
    for q in questions["ask"]:
        if q.get("cross_language"):
            by_text[q["variants"][0]["text"]] = key[f"{q['id']}__en"]
            assert key[f"{q['id']}__en"] == key[f"{q['id']}__fr"]
        else:
            by_text[q["text"]] = key[q["id"]]
    gen = small.generation
    assert key["_generation"] == {
        "corpus": "synthetic",
        "generation": gen,
        "documents": 36,
        "sidecars": 36,
        "unusable_sidecars": 1,
        "questions": 20,
        "built_by": "scripts.test_corpora.corpora.synthetic_key",
    }
    assert [q["id"] for q in questions["ask"]] == [f"synthetic-key-{i}" for i in range(1, 21)]
    assert all(e["generation"] == gen for qid, e in key.items() if qid != "_generation")
    # One representative invoice per vendor, by stem, with an invoice number no other invoice carries, kept as
    # text, its key in its text: A-2 is two invoices, B-1 is a PDF, B-2's text does not name its vendor.
    assert by_text["What is the total amount due on invoice A-1 from Alpha Supplies to Marbledock & Associates?"] == {
        "kind": "amount",
        "amount": 1000.5,
        "document": "0001_invoice",
        "generation": gen,
    }
    assert by_text["Which vendor issued invoice B-3 to Marbledock & Associates?"] == {
        "kind": "fact",
        "all_of": [["Beta Software"]],  # "Beta" is four letters, too short to stand alone
        "document": "0006_invoice",
        "generation": gen,
    }
    assert not [t for t in by_text if "A-2" in t or "B-1" in t or "B-2" in t]
    # The count goes to the vendor with the fewest invoices (a tie by name), the total to the one with the most, the
    # month total to the month with the fewest invoices of two or more.
    count = by_text["How many invoices did Alpha Supplies issue to Marbledock & Associates in 2025?"]
    assert count == {
        "kind": "count",
        "count": 3,
        "field": "vendor",
        "documents": ["0001_invoice", "0002_invoice", "0003_invoice"],
        "generation": gen,
    }
    total = by_text[
        "What is the total amount invoiced by Beta Software to Marbledock & Associates in 2025, across all its invoices?"
    ]
    assert (total["amount"], total["documents"]) == (2050.25, ["0004_invoice", "0005_invoice", "0006_invoice"])
    march = by_text["What was the total amount invoiced to Marbledock & Associates in March 2025, across all vendors?"]
    assert (march["amount"], march["documents"]) == (3000.5, ["0001_invoice", "0002_invoice"])
    # Lists: the same vendor's invoices and the busiest month's, over every invoice, with aliases only where unique:
    # A-2 names two invoices and so does $2,000 in whole dollars, so those two are named by their cents.
    invoices = by_text[
        "List every invoice Alpha Supplies issued to Marbledock & Associates in 2025, with its invoice number and total."
    ]
    assert invoices["positives"] == ["0001_invoice", "0002_invoice", "0003_invoice"] and len(invoices["universe"]) == 6
    assert invoices["aliases"] == {
        "0001_invoice": ["A-1", "1000.50", "1000"],
        "0002_invoice": ["2000.00"],
        "0003_invoice": ["2000.75"],
        "0004_invoice": ["B-1", "500.25", "500"],
        "0005_invoice": ["B-2", "750.00", "750"],
        "0006_invoice": ["B-3", "800.00", "800"],
    }
    february = by_text[
        "List every invoice issued to Marbledock & Associates in February 2025, from any vendor, with its invoice number and total."
    ]
    assert february["positives"] == ["0004_invoice", "0005_invoice", "0006_invoice"] and february["field"] == "date"
    # A contract is named by vendor and term when no other contract shares them; the first gets its fee, the
    # second its law, with "State of" or "Province of" dropped from the key. The failed sidecar is not a contract.
    fee = by_text[
        "What is the monthly fee in the 12-month vendor service contract between Marbledock & Associates and Alpha Supplies?"
    ]
    assert (fee["amount"], fee["document"]) == (4500, "0007_vendor_contract")
    law = by_text[
        "What is the governing law of the 24-month vendor service contract between Marbledock & Associates and Beta Software?"
    ]
    assert (law["all_of"], law["document"]) == ([["Ontario"]], "0010_vendor_contract")
    assert not [t for t in by_text if "12-month vendor service contract between Marbledock & Associates and Beta" in t]
    # An onboarding letter is asked about when it is addressed to one person by name: not a template's "New Team
    # Member", not a name two letters share. The first named person's start date is the bilingual pair; the second's
    # signer, read from a dict as readily as from a string.
    start = by_text[
        "On what date does Élise Fontaine start at Marbledock & Associates, according to the onboarding letter?"
    ]
    assert start == {
        "kind": "date",
        "date": "2025-12-01",
        "day_first": True,
        "document": "0013_onboarding_letter",
        "generation": gen,
    }
    assert by_text["Who signed the onboarding letter addressed to Alex Thibodeau?"]["all_of"] == [
        ["Madeleine Côté-Renaud"]
    ]
    assert not [t for t in by_text if "Jean Roy" in t or "New Team Member" in t]
    # A policy is named by name and version when no other policy shares them, the words of the name in any order,
    # "&" as "and", "v2.1" as "2.1.0". The first gets its owner, the second its effective date.
    assert not [t for t in by_text if "Information Security" in t]
    owner = by_text["Who is the policy owner of version 3.0 of the Travel Policy?"]
    assert owner["all_of"] == [["Finance Department"]] and owner["document"] == "0019_policy_doc"
    assert by_text["On what date did version 3.1 of the Travel Policy take effect?"]["date"] == "2025-11-02"
    # A memo by its subject, when no other memo has it (case aside); the sender is the part before the comma.
    memo = by_text["Who sent the internal memo with the subject “Parking”?"]
    assert memo["all_of"] == [["Office of the Managing Partner"]] and memo["document"] == "0023_internal_memo"
    assert not [t for t in by_text if "Remote" in t]
    # Board minutes: a date for each of the first two months with one meeting (August's is a PDF and the third
    # anyway), a count for the quarter with the most meetings.
    assert (
        by_text["On what date did the board of Marbledock & Associates meet in January 2025?"]["date"] == "2025-01-12"
    )
    assert by_text["On what date did the board of Marbledock & Associates meet in July 2025?"]["date"] == "2025-07-01"
    assert not [t for t in by_text if "August 2025" in t]
    q3 = by_text["How many board meetings did Marbledock & Associates hold in Q3 2025, according to the board minutes?"]
    assert (q3["count"], q3["documents"]) == (
        4,
        ["0027_board_minutes", "0028_board_minutes", "0029_board_minutes", "0030_board_minutes"],
    )
    # Marketing: campaigns by brief count, then name; a count for the first, the targets of the second as a bilingual
    # list over every client, and the campaigns of the client the fewest campaigns target.
    assert (
        by_text["How many marketing briefs did Marbledock & Associates write for the Northstar campaign?"]["count"] == 3
    )
    targets = by_text["Which clients do the marketing briefs for the Skylight campaign target?"]
    assert (targets["positives"], targets["universe"]) == (
        ["Northwind Partners", "Polestar Industries"],
        ["Acme Corp", "Northwind Partners", "Polestar Industries"],
    )
    acme = by_text["Which campaigns have a marketing brief that targets Acme Corp?"]
    assert (acme["positives"], acme["universe"]) == (["Harbour Lights"], ["Harbour Lights", "Northstar", "Skylight"])
    # What the build read is what a snapshot must hold: every text it checked a key against, kept or rejected.
    assert small.texts_read == {
        "0001_invoice", "0005_invoice", "0006_invoice", "0007_vendor_contract", "0010_vendor_contract",
        "0013_onboarding_letter", "0014_onboarding_letter", "0019_policy_doc", "0020_policy_doc",
        "0023_internal_memo", "0024_board_minutes", "0027_board_minutes",
    }  # fmt: skip


def test_the_same_sidecars_give_the_same_key_and_the_snapshot_round_trips(small, tmp_path):
    questions, key = build(small)
    assert build(Corpus.load(small.root)) == (questions, key)
    snapshot = tmp_path / "snapshot"
    write_snapshot(small, snapshot)
    assert sorted(p.name for p in (snapshot / "ingest").iterdir()) == sorted(f"{s}.txt" for s in small.texts_read)
    assert len(list((snapshot / "facts").glob("*.json"))) == 36 and (snapshot / "ingest_names.txt").exists()
    again = Corpus.load(snapshot)
    assert again.ingest_names == small.ingest_names and again.unusable == ["0011_vendor_contract"]
    assert build(again) == (questions, key), "the snapshot is enough to rebuild the key"
    # A snapshot missing a text the build reads says so instead of silently building another key.
    (snapshot / "ingest" / "0005_invoice.txt").unlink()
    with pytest.raises(SnapshotIncomplete, match="0005_invoice.txt is listed but not present"):
        build(Corpus.load(snapshot))


def test_the_keys_of_the_small_corpus_score_their_own_documents(small, tmp_path):
    answer_key.bind_corpus_root(tmp_path)
    _, key = build(small)
    for qid, e in key.items():
        if qid != "_generation" and "document" in e:
            assert score(e, small.text(e["document"]))["score"] == 1.0, qid
    # French and English answers to the pair both score against its one key.
    pair = key["synthetic-key-10__fr"]
    assert score(pair, "Élise Fontaine entre en fonction le 1er décembre 2025.")["score"] == 1.0
    assert score(pair, "Her start date is December 1, 2025 (01/12/2025).")["score"] == 1.0
    assert score(key["synthetic-key-3__fr"], "Alpha Supplies a émis trois factures.")["score"] == 1.0
    assert score(key["synthetic-key-3__en"], "Alpha Supplies issued 3 invoices: A-1, A-2 and A-2.")["score"] == 1.0
    assert score(key["synthetic-key-3__fr"], "Je n'ai trouvé aucune information; peut-être 3 factures.")["score"] == 0.0


def test_verify_aggregates_names_the_contributor_whose_text_lacks_its_value(small):
    _, key = build(small)
    problems = verify_aggregates(small, key)
    # 0004 is a PDF; 0002's text states its total; the memo and board PDFs likewise; the briefs' texts are stubs that
    # do not name their campaign or target.
    assert "synthetic-key-4 0004_invoice: rendered to PDF, not checked" in problems
    assert "synthetic-key-5 0002_invoice: total_usd 2000.0 not in its text" not in problems
    assert "synthetic-key-17 0028_board_minutes: rendered to PDF, not checked" in problems
    assert "synthetic-key-18 0034_marketing_brief: campaign_name 'Northstar' not in its text" in problems
    assert "synthetic-key-19__en 0031_marketing_brief: target 'Northwind Partners' not in its text" in problems
    assert not [p for p in problems if p.startswith("synthetic-key-1 ") or p.startswith("synthetic-key-2 ")]


def test_the_cli_refuses_to_overwrite_without_redo(small, tmp_path, capsys):
    out, snap = tmp_path / "out", tmp_path / "snap"
    args = ["--corpus-dir", str(small.root), "--out-dir", str(out), "--snapshot", str(snap)]
    assert synthetic_key.main(args) == 0
    assert (out / "synthetic.yaml").exists() and (out / "synthetic.key.json").exists() and (snap / "facts").is_dir()
    assert "20 questions (23 key entries) for synthetic generation" in capsys.readouterr().out
    assert synthetic_key.main(args) == 2
    assert "refusing to overwrite without --redo" in capsys.readouterr().err
    (out / "synthetic.yaml").write_text("stale\n")
    assert synthetic_key.main([*args, "--redo", "--verify-aggregates"]) == 0
    assert yaml.safe_load((out / "synthetic.yaml").read_text())["ask"][0]["id"] == "synthetic-key-1"
    assert "aggregate contributor: synthetic-key-4 0004_invoice: rendered to PDF" in capsys.readouterr().out
    # --workdir names the sweep's workdir, under which the corpus is synthetic/.
    assert synthetic_key.main(["--workdir", str(small.root.parent), "--out-dir", str(out), "--redo"]) == 0


def test_sidecar_values_the_generator_shaped_differently():
    assert synthetic_key._name({"name": "Sophie Trépanier", "title": "Directrice"}) == "Sophie Trépanier"
    assert synthetic_key._name({"signatory_name": "Jonathan R. Aldridge"}) == "Jonathan R. Aldridge"
    assert synthetic_key._name("Mme. Isabelle Fontaine, Directrice des Opérations") == "Isabelle Fontaine"
    assert synthetic_key._name("") is None and synthetic_key._name(None) is None and synthetic_key._name({}) is None
    assert synthetic_key._version_key("v2.1") == synthetic_key._version_key("2.1.0") == "2.1"
    assert synthetic_key._version_key("3.2.1") == "3.2.1" and synthetic_key._version_key("draft") is None
    assert synthetic_key._name_key("A, B & C Policy") == synthetic_key._name_key("C, B and A Policy")
    assert synthetic_key._person_name("Élise Fontaine") == "Élise Fontaine"
    for placeholder in ("New Team Member", "Nouvelle recrue / New Hire", "[Employee Name]", "Not specified", "Alex"):
        assert synthetic_key._person_name(placeholder) is None, placeholder
    assert synthetic_key._number("$12,500") == 12500 and synthetic_key._number(True) is None
    assert synthetic_key._number("twelve") is None and synthetic_key._number(None) is None
    assert synthetic_key._owner_alternatives("HR & IT Department, Marbledock & Associates") == [
        "HR & IT Department, Marbledock & Associates",
        "HR and IT Department, Marbledock and Associates",
        "HR & IT Department",
        "HR and IT Department",
    ]
    assert synthetic_key._vendor_alternatives("Globex Supplies", ["Globex Supplies", "Initech Software"]) == [
        "Globex Supplies",
        "Globex",
    ]
    assert synthetic_key._vendor_alternatives("Acme Corp", ["Acme Corp", "Acme Industries"]) == ["Acme Corp"]
