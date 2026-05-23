# scripts/test_corpora/tests/test_generate_enron.py
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.groundtruth.generate_enron import (
    _find_item,
    _grep,
    _parse_email_date,
    generate,
)


def _write_eml(
    path: Path,
    *,
    from_: str = "x@y.com",
    to: str = "a@b.com",
    subject: str = "test",
    date: str = "Wed, 14 Aug 2001 09:00:00 -0500",
    body: str = "body text",
) -> None:
    path.write_text(
        f"From: {from_}\nTo: {to}\nSubject: {subject}\nDate: {date}\n\n{body}",
    )


def test_grep_returns_sorted_filenames_case_insensitive(tmp_path: Path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_eml(ingest / "a.eml", body="mentions Raptor in body")
    _write_eml(ingest / "b.eml", body="no match here")
    _write_eml(ingest / "c.eml", body="RAPTOR is in caps")
    matches = _grep(ingest, "raptor")
    assert matches == ["a.eml", "c.eml"]


def test_grep_returns_empty_list_for_absent_term(tmp_path: Path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_eml(ingest / "a.eml", body="nothing relevant")
    assert _grep(ingest, "cryptocurrency") == []


def test_parse_email_date_filters_pre_1995_sentinel(tmp_path: Path):
    p = tmp_path / "bad.eml"
    _write_eml(p, date="Tue, 1 Jan 1980 00:00:00 +0000")
    assert _parse_email_date(p) is None


def test_parse_email_date_returns_real_dates(tmp_path: Path):
    p = tmp_path / "ok.eml"
    _write_eml(p, date="Wed, 14 Aug 2001 09:00:00 -0500")
    dt = _parse_email_date(p)
    assert dt is not None
    assert dt.year == 2001 and dt.month == 8 and dt.day == 14


def test_parse_email_date_handles_missing_or_malformed(tmp_path: Path):
    p = tmp_path / "nodate.eml"
    p.write_text("From: x@y\nSubject: no date\n\nbody")
    assert _parse_email_date(p) is None


def test_find_item_builds_count_all_sample_shape():
    item = _find_item("enron-find-x", "Find x.", all_matches=["b.eml", "a.eml", "c.eml"])
    assert item["type"] == "find"
    assert item["id"] == "enron-find-x"
    assert item["question"] == "Find x."
    assert item["answer_key"]["count"] == 3
    # `all` is preserved in the order passed in (the caller sorts); sample takes first K
    assert item["answer_key"]["all"] == ["b.eml", "a.eml", "c.eml"]
    assert item["answer_key"]["sample"] == ["b.eml", "a.eml", "c.eml"]


def test_generate_end_to_end_with_minimal_fixture(tmp_path: Path):
    """Build a small fixture that satisfies every recipe; assert all 11 items."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    # Cover each recipe with at least one matching email:
    _write_eml(
        ingest / "skilling-j_doc_1.eml",
        from_="jeff.skilling@enron.com",
        subject="Pre-resignation note about CA",
        date="Wed, 1 Aug 2001 09:00:00 -0500",
        body="The California state of the union is concerning. Also: raptor.",
    )
    _write_eml(
        ingest / "lay-k_inbox_1.eml",
        from_="kenneth.lay@enron.com",
        subject="Fwd: business update",
        date="Mon, 6 Aug 2001 12:00:00 -0500",
        body="LJM and off-balance-sheet structures with FERC oversight; Arthur Andersen too.",
    )
    _write_eml(
        ingest / "neutral_doc_1.eml",
        subject="weather",
        date="Fri, 1 Jun 2001 09:00:00 -0500",
        body="just neutral content",
    )
    out = tmp_path / "enron.yaml"
    n = generate(ingest_dir=ingest, out_path=out)
    assert n == 11

    data = yaml.safe_load(out.read_text())
    assert data["corpus"] == "enron"
    by_id = {i["id"]: i for i in data["items"]}

    # All expected ids are present
    expected = {
        "enron-find-raptor",
        "enron-find-ljm",
        "enron-find-offbalancesheet",
        "enron-find-arthurandersen",
        "enron-find-ferc",
        "enron-find-layforwarded2001",
        "enron-lookup-earliest-california",
        "enron-lookup-skilling-last-pre-resign",
        "enron-find-neg-cryptocurrency",
        "enron-find-neg-bitcoin",
        "enron-find-neg-spacex",
    }
    assert set(by_id) == expected

    # find items have dict answer_keys with count/all/sample
    for fid in [k for k in by_id if k.startswith("enron-find-") and "neg" not in k]:
        ak = by_id[fid]["answer_key"]
        assert set(ak) == {"count", "all", "sample"}

    # Raptor item picked up the right file
    assert by_id["enron-find-raptor"]["answer_key"]["count"] == 1
    assert by_id["enron-find-raptor"]["answer_key"]["all"] == ["skilling-j_doc_1.eml"]

    # Lookup items
    assert by_id["enron-lookup-earliest-california"]["answer_key"] == "2001-08-01"
    assert by_id["enron-lookup-skilling-last-pre-resign"]["answer_key"] == "Pre-resignation note about CA"

    # Negatives: count=0, all=[], sample=[]
    for nid in ["enron-find-neg-cryptocurrency", "enron-find-neg-bitcoin", "enron-find-neg-spacex"]:
        assert by_id[nid]["answer_key"] == {"count": 0, "all": [], "sample": []}
        assert by_id[nid]["type"] == "find"


def test_generate_raises_when_negative_term_has_hits(tmp_path: Path):
    """The generator refuses to emit a negative whose term unexpectedly matches."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_eml(ingest / "a.eml", body="The cryptocurrency market is heating up.")
    out = tmp_path / "enron.yaml"
    with pytest.raises(RuntimeError, match="negative term .* unexpectedly has"):
        generate(ingest_dir=ingest, out_path=out)


def test_grep_does_not_recurse_into_subdirectories(tmp_path: Path):
    """_grep is flat — files in subdirectories are NOT searched. The Enron
    corpus uses one-level filenames-encode-folder names (e.g.
    `lay-k_inbox_42_.eml`); a recursive sweep returning only basenames would
    silently collide on identical filenames in different subdirs."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    sub = ingest / "nested"
    sub.mkdir()
    _write_eml(ingest / "top.eml", body="raptor at top")
    _write_eml(sub / "nested.eml", body="raptor in nested dir, should not be found")
    assert _grep(ingest, "raptor") == ["top.eml"]
