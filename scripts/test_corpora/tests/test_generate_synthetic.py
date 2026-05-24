# scripts/test_corpora/tests/test_generate_synthetic.py
import json
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.groundtruth.generate_synthetic import (
    _find_item,
    _iter_sidecars,
    _load_sidecar,
    _lookup_item,
    _to_title,
    generate,
)

# ── fixture helpers ─────────────────────────────────────────────────────────


def _write_doc(ingest: Path, stem: str, sidecar: dict, text: str = "rendered body") -> None:
    """Write a synthetic doc pair: the .json sidecar + the .txt rendering."""
    (ingest / f"{stem}.json").write_text(json.dumps(sidecar))
    (ingest / f"{stem}.txt").write_text(text)


def _make_full_fixture(tmp_path: Path) -> Path:
    """Build a minimal fixture covering all 9 doc-types so generate() can run end-to-end."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_doc(
        ingest,
        "0001_invoice",
        {
            "vendor": "Acme Supplies",
            "invoice_number": "ACM-001",
            "date": "2025-01-15",
            "total_usd": 1234.56,
            "line_items": [],
        },
    )
    _write_doc(
        ingest,
        "0002_invoice",
        {
            "vendor": "Globex Supplies",
            "invoice_number": "GLO-002",
            "date": "2025-02-20",
            "total_usd": 7890.12,
            "line_items": [],
        },
    )
    _write_doc(
        ingest,
        "0010_board_minutes",
        {
            "date": "2025-03-01",
            "attendees": ["Alice", "Bob", "Carol"],
            "decisions": ["Approved budget.", "Authorized hire."],
            "lang": "en",
        },
    )
    _write_doc(
        ingest,
        "0011_board_minutes",
        {
            "date": "2025-04-02",
            "attendees": ["Alice", "Bob"],
            "decisions": ["Tabled motion."],
            "lang": "fr",  # French-tagged for FR coverage
        },
    )
    _write_doc(
        ingest,
        "0020_onboarding_letter",
        {
            "employee_name": "n/a",
            "role": "Senior Engineer",
            "start_date": "2025-05-01",
            "languages_used": ["English"],
            "signing_manager": "Sophie Tran-Beaumont",
        },
    )
    _write_doc(
        ingest,
        "0030_quarterly_report",
        {
            "quarter": "Q2",
            "year": 2025,
            "revenue_usd": 4275000,
            "key_initiatives": {},
        },
    )
    _write_doc(
        ingest,
        "0040_vendor_contract",
        {
            "vendor": "Pinnacle Tech Solutions, LLC",
            "term_months": 24,
            "monthly_fee_usd": 8500.0,
            "governing_law": "State of South Carolina",
            "signatures": {},
        },
    )
    _write_doc(
        ingest,
        "0050_internal_memo",
        {
            "from": "Helena Voss, COO",
            "to": "All Staff",
            "subject": "Updated Remote Work Policy",
            "lang": "en",
        },
    )
    _write_doc(
        ingest,
        "0060_policy_doc",
        {
            "policy_name": "Information Security Policy",
            "version": "3.1",
            "effective_date": "2025-12-23",
            "owner": "CHRO & CISO",
        },
    )
    _write_doc(
        ingest,
        "0070_marketing_brief",
        {
            "campaign_name": "Marbledock Elevate 2025",
            "target": "Mid-market firms",
            "budget_usd": 120000,
            "owner": "Senior Director of Marketing",
        },
    )
    _write_doc(
        ingest,
        "0080_employee_handbook",
        {
            "year": 2025,
            "sections": ["3.1 Professional Standards", "3.2 Conflict of Interest"],
            "lang_split": {"primary": "English", "secondary": "French", "total_section_count": 4},
        },
    )
    return ingest


# ── helper tests ────────────────────────────────────────────────────────────


def test_to_title_strips_known_extensions():
    """_to_title strips .json / .txt / .pdf to match HC's doc_title format."""
    assert _to_title("0019_invoice.json") == "0019_invoice"
    assert _to_title("0019_invoice.txt") == "0019_invoice"
    assert _to_title("0019_invoice.pdf") == "0019_invoice"
    assert _to_title("0019_invoice") == "0019_invoice"  # already a stem


def test_load_sidecar_reads_json(tmp_path: Path):
    p = tmp_path / "0019_invoice.json"
    p.write_text(json.dumps({"vendor": "Acme", "total_usd": 100}))
    sc = _load_sidecar(p)
    assert sc == {"vendor": "Acme", "total_usd": 100}


def test_iter_sidecars_filters_by_doctype_and_sorts(tmp_path: Path):
    """_iter_sidecars yields (stem, sidecar) for matching docs, sorted by stem."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_doc(ingest, "0002_invoice", {"vendor": "B"})
    _write_doc(ingest, "0001_invoice", {"vendor": "A"})
    _write_doc(ingest, "0003_board_minutes", {"date": "2025-01-01"})
    pairs = list(_iter_sidecars(ingest, "invoice"))
    assert [stem for stem, _ in pairs] == ["0001_invoice", "0002_invoice"]
    assert pairs[0][1]["vendor"] == "A"


def test_lookup_item_builds_canonical_shape():
    item = _lookup_item(
        id_="synth-test-1",
        question="q?",
        gold_doc="0001_invoice",
        answer_key="A",
        clause_category="invoice",
    )
    assert item == {
        "id": "synth-test-1",
        "question": "q?",
        "clause_category": "invoice",
        "gold_doc": "0001_invoice",
        "answer_key": "A",
        "type": "lookup",
    }


def test_find_item_builds_count_all_sample_shape():
    item = _find_item(
        id_="synth-find-1",
        question="Find docs",
        all_matches=["b", "a", "c"],
        clause_category="cross-doc",
    )
    assert item["type"] == "find"
    assert item["answer_key"] == {"count": 3, "all": ["b", "a", "c"], "sample": ["b", "a", "c"]}
    assert item["clause_category"] == "cross-doc"


# ── end-to-end generator tests ──────────────────────────────────────────────


def test_generate_emits_items_across_all_doc_types(tmp_path: Path):
    """The orchestrator emits items for every doc-type plus cross-doc + negatives."""
    ingest = _make_full_fixture(tmp_path)
    out = tmp_path / "synthetic.yaml"
    n = generate(ingest_dir=ingest, out_path=out)

    data = yaml.safe_load(out.read_text())
    assert data["corpus"] == "synthetic"
    items = data["items"]
    assert len(items) == n
    assert n >= 14  # 9 doc-types × ≥1 each + cross-doc + negatives + FR

    categories = {i["clause_category"] for i in items}
    expected_doc_types = {
        "invoice",
        "board_minutes",
        "onboarding_letter",
        "quarterly_report",
        "vendor_contract",
        "internal_memo",
        "policy_doc",
        "marketing_brief",
        "employee_handbook",
    }
    assert expected_doc_types.issubset(categories), f"missing doc-types: {expected_doc_types - categories}"

    types = {i["type"] for i in items}
    assert "lookup" in types
    assert "find" in types  # cross-doc finds + find-negative
    assert "negative" in types  # lookup-negative

    # Cross-doc find: invoices over $5000 — only 0002 (7890.12) qualifies
    inv_find = next(i for i in items if i["id"] == "synth-find-invoices-over-5000")
    assert inv_find["type"] == "find"
    assert inv_find["answer_key"]["all"] == ["0002_invoice"]
    assert inv_find["answer_key"]["count"] == 1

    # Cross-doc find: Q4 2025 policies — only 0060_policy_doc (effective 2025-12-23) qualifies
    q4_find = next(i for i in items if i["id"] == "synth-find-policies-q4-2025")
    assert q4_find["type"] == "find"
    assert q4_find["answer_key"]["all"] == ["0060_policy_doc"]
    assert q4_find["answer_key"]["count"] == 1

    # Lookup-negative: a non-existent invoice number
    neg = next(i for i in items if i["id"] == "synth-neg-invoice-99999")
    assert neg["type"] == "negative"
    assert neg["answer_key"] is None

    # find-negative: a non-existent vendor
    fneg = next(i for i in items if i["id"] == "synth-find-neg-vendor-globex-aerospace")
    assert fneg["type"] == "find"
    assert fneg["answer_key"] == {"count": 0, "all": [], "sample": []}


def test_generate_emits_french_items_when_fr_sidecars_present(tmp_path: Path):
    """When the corpus has FR-tagged sidecars (lang: fr), the generator emits FR items."""
    ingest = _make_full_fixture(tmp_path)  # 0011_board_minutes has lang: fr
    out = tmp_path / "synthetic.yaml"
    generate(ingest_dir=ingest, out_path=out)
    data = yaml.safe_load(out.read_text())
    fr_items = [i for i in data["items"] if i["id"].startswith("synth-fr-")]
    assert len(fr_items) >= 1, "no FR items emitted despite FR-tagged sidecars in fixture"
    fr = fr_items[0]
    # The question text should contain at least one French-distinctive character/word.
    # Use a permissive heuristic: any of "é", "è", "ê", "à", "ç", or the French article "le"/"la".
    assert any(tok in fr["question"] for tok in ("é", "è", "ê", "à", "ç", " le ", " la ", " du ")), (
        f"FR item question doesn't look French: {fr['question']!r}"
    )


def test_generate_raises_when_negative_vendor_unexpectedly_matches(tmp_path: Path):
    """If a docs in the corpus actually has the negative vendor name, generate refuses."""
    ingest = _make_full_fixture(tmp_path)
    # Plant a doc that contains "Globex Aerospace" — the negative target.
    _write_doc(
        ingest,
        "0099_vendor_contract",
        {
            "vendor": "Globex Aerospace",
            "term_months": 6,
            "monthly_fee_usd": 1000.0,
            "governing_law": "n/a",
            "signatures": {},
        },
        text="contract with Globex Aerospace for services",
    )
    out = tmp_path / "synthetic.yaml"
    with pytest.raises(RuntimeError, match="negative vendor 'Globex Aerospace' unexpectedly matches"):
        generate(ingest_dir=ingest, out_path=out)


def test_generate_silently_skips_items_with_duplicate_questions(tmp_path: Path, capsys):
    """When two sidecars yield identical question text, the orchestrator keeps
    the first and drops the rest — the model can't disambiguate by anything
    other than question text, so a dup is an eval-validity bug; silent skip
    + a stderr warning is the right default since some corpus quirks (e.g.,
    all employee_handbook sidecars share `year: 2025`) make dup-emission
    unavoidable without per-recipe hand-curation.

    Reproducer: overwrite 0002_invoice to share 0001_invoice's invoice_number,
    so the lookup-by-number recipe emits the same question text for both
    sidecars. The 0001 item survives; the 0002 item is dropped."""
    ingest = _make_full_fixture(tmp_path)
    (ingest / "0002_invoice.json").write_text(
        json.dumps(
            {
                "vendor": "Other Vendor",
                "invoice_number": "ACM-001",  # collides with 0001_invoice's number
                "date": "2025-02-20",
                "total_usd": 100.0,
                "line_items": [],
            }
        )
    )
    out = tmp_path / "synthetic.yaml"
    generate(ingest_dir=ingest, out_path=out)
    data = yaml.safe_load(out.read_text())

    # Both invoice recipes key their question on invoice_number, so when 0002
    # collides with 0001's number, BOTH 0002 items get dropped — only the
    # 0001 lookups survive. (Filter by type to exclude the negative item,
    # which also has clause_category="invoice".)
    invoice_lookups = [i for i in data["items"] if i["clause_category"] == "invoice" and i["type"] == "lookup"]
    assert len(invoice_lookups) == 2
    assert all(i["gold_doc"] == "0001_invoice" for i in invoice_lookups)

    # A stderr warning records how many items got skipped — at least 2 (the
    # 0002 total + vendor recipes that both collided on invoice number).
    err = capsys.readouterr().err
    assert "skipped" in err and "duplicated" in err


def test_generate_raises_when_negative_invoice_number_unexpectedly_matches(tmp_path: Path):
    """If a doc in the corpus actually uses invoice_number INV-99999, generate refuses."""
    ingest = _make_full_fixture(tmp_path)
    _write_doc(
        ingest,
        "0098_invoice",
        {
            "vendor": "x",
            "invoice_number": "INV-99999",
            "date": "2025-01-01",
            "total_usd": 1,
            "line_items": [],
        },
    )
    out = tmp_path / "synthetic.yaml"
    with pytest.raises(RuntimeError, match="negative invoice_number 'INV-99999' unexpectedly matches"):
        generate(ingest_dir=ingest, out_path=out)
