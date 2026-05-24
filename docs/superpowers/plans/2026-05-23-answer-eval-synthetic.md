# Synthetic Answer-Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `--mode answer-eval` to the Synthetic (Marbledock & Associates) corpus by adding a sidecar-driven ground-truth generator that produces ~20 items across all 9 doc-types, with 2–3 French-language items for bilingual coverage.

**Architecture:** Additive only — one new generator (`generate_synthetic.py`) + one new frozen artifact (`synthetic.yaml`). Zero changes to the judge, runner, sweep, or `GTItem` schema. The generator dispatches per-doc-type to a recipe function that reads the canonical JSON sidecar and emits items; cross-doc finds + negatives are hand-coded; FR-tagged sidecars are picked by a small scan.

**Tech Stack:** Python 3.12, `pyyaml`, stdlib `json`/`pathlib`, `pytest`. All work is in `scripts/test_corpora/`. Run tests with `uv run --project scripts/test_corpora --extra test pytest <path>` from the repo root.

**Spec:** `docs/superpowers/specs/2026-05-23-answer-eval-synthetic-design.md`.

**File map:**
- Create `scripts/test_corpora/groundtruth/generate_synthetic.py` — Synthetic ground-truth generator.
- Create `scripts/test_corpora/groundtruth/synthetic.yaml` — frozen ~20-item ground-truth set (generated, then committed).
- Create `scripts/test_corpora/tests/test_generate_synthetic.py` — generator tests.

---

## Task 1: `generate_synthetic.py` — helpers, recipes, orchestrator

The single substantive code task. Builds helpers (`_to_title`, `_load_sidecar`, `_iter_sidecars`, `_lookup_item`, `_find_item`), 9 per-doc-type recipes, 2 cross-doc `find` recipes, 2 negative items, the FR-item scanner, the `generate()` orchestrator, and a CLI.

**Files:**
- Create: `scripts/test_corpora/groundtruth/generate_synthetic.py`
- Create: `scripts/test_corpora/tests/test_generate_synthetic.py`

- [ ] **Step 1: Write the failing tests** — create `scripts/test_corpora/tests/test_generate_synthetic.py`:

```python
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
    _write_doc(ingest, "0001_invoice", {
        "vendor": "Acme Supplies", "invoice_number": "ACM-001",
        "date": "2025-01-15", "total_usd": 1234.56, "line_items": [],
    })
    _write_doc(ingest, "0002_invoice", {
        "vendor": "Globex Supplies", "invoice_number": "GLO-002",
        "date": "2025-02-20", "total_usd": 7890.12, "line_items": [],
    })
    _write_doc(ingest, "0010_board_minutes", {
        "date": "2025-03-01", "attendees": ["Alice", "Bob", "Carol"],
        "decisions": ["Approved budget.", "Authorized hire."], "lang": "en",
    })
    _write_doc(ingest, "0011_board_minutes", {
        "date": "2025-04-02", "attendees": ["Alice", "Bob"],
        "decisions": ["Tabled motion."], "lang": "fr",  # French-tagged for FR coverage
    })
    _write_doc(ingest, "0020_onboarding_letter", {
        "employee_name": "n/a", "role": "Senior Engineer",
        "start_date": "2025-05-01", "languages_used": ["English"],
        "signing_manager": "Sophie Tran-Beaumont",
    })
    _write_doc(ingest, "0030_quarterly_report", {
        "quarter": "Q2", "year": 2025, "revenue_usd": 4275000,
        "key_initiatives": {},
    })
    _write_doc(ingest, "0040_vendor_contract", {
        "vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24,
        "monthly_fee_usd": 8500.0, "governing_law": "State of South Carolina",
        "signatures": {},
    })
    _write_doc(ingest, "0050_internal_memo", {
        "from": "Helena Voss, COO", "to": "All Staff",
        "subject": "Updated Remote Work Policy", "lang": "en",
    })
    _write_doc(ingest, "0060_policy_doc", {
        "policy_name": "Information Security Policy", "version": "3.1",
        "effective_date": "2025-12-23", "owner": "CHRO & CISO",
    })
    _write_doc(ingest, "0070_marketing_brief", {
        "campaign_name": "Marbledock Elevate 2025",
        "target": "Mid-market firms", "budget_usd": 120000,
        "owner": "Senior Director of Marketing",
    })
    _write_doc(ingest, "0080_employee_handbook", {
        "year": 2025,
        "sections": ["3.1 Professional Standards", "3.2 Conflict of Interest"],
        "lang_split": {"primary": "English", "secondary": "French",
                       "total_section_count": 4},
    })
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
        id_="synth-test-1", question="q?", gold_doc="0001_invoice",
        answer_key="A", clause_category="invoice",
    )
    assert item == {
        "id": "synth-test-1", "question": "q?",
        "clause_category": "invoice", "gold_doc": "0001_invoice",
        "answer_key": "A", "type": "lookup",
    }


def test_find_item_builds_count_all_sample_shape():
    item = _find_item(
        id_="synth-find-1", question="Find docs", all_matches=["b", "a", "c"],
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
        "invoice", "board_minutes", "onboarding_letter", "quarterly_report",
        "vendor_contract", "internal_memo", "policy_doc", "marketing_brief",
        "employee_handbook",
    }
    assert expected_doc_types.issubset(categories), (
        f"missing doc-types: {expected_doc_types - categories}"
    )

    types = {i["type"] for i in items}
    assert "lookup" in types
    assert "find" in types  # cross-doc finds + find-negative
    assert "negative" in types  # lookup-negative

    # Cross-doc find: invoices over $5000 — only 0002 (7890.12) qualifies
    inv_find = next(i for i in items if i["id"] == "synth-find-invoices-over-5000")
    assert inv_find["type"] == "find"
    assert inv_find["answer_key"]["all"] == ["0002_invoice"]
    assert inv_find["answer_key"]["count"] == 1

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
        ingest, "0099_vendor_contract",
        {"vendor": "Globex Aerospace", "term_months": 6, "monthly_fee_usd": 1000.0,
         "governing_law": "n/a", "signatures": {}},
        text="contract with Globex Aerospace for services",
    )
    out = tmp_path / "synthetic.yaml"
    with pytest.raises(RuntimeError, match="negative vendor 'Globex Aerospace' unexpectedly matches"):
        generate(ingest_dir=ingest, out_path=out)


def test_generate_raises_when_negative_invoice_number_unexpectedly_matches(tmp_path: Path):
    """If a doc in the corpus actually uses invoice_number INV-99999, generate refuses."""
    ingest = _make_full_fixture(tmp_path)
    _write_doc(ingest, "0098_invoice", {
        "vendor": "x", "invoice_number": "INV-99999",
        "date": "2025-01-01", "total_usd": 1, "line_items": [],
    })
    out = tmp_path / "synthetic.yaml"
    with pytest.raises(RuntimeError, match="negative invoice_number 'INV-99999' unexpectedly matches"):
        generate(ingest_dir=ingest, out_path=out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_generate_synthetic.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.test_corpora.groundtruth.generate_synthetic'`.

- [ ] **Step 3: Create the generator** — create `scripts/test_corpora/groundtruth/generate_synthetic.py` with exactly this content:

```python
# scripts/test_corpora/groundtruth/generate_synthetic.py
"""Generate the Synthetic answer-eval ground-truth set from per-doc JSON sidecars.

The synthetic corpus (fictional company Marbledock & Associates) is authored by
``corpora/synthetic.py``, which writes a JSON sidecar per document carrying the
canonical, structured ground-truth facts (vendor, dates, totals, signatories,
etc.). This generator dispatches by doc-type (from the filename suffix) to a
recipe function that lifts those sidecar fields into ground-truth Q&A items.

Cross-doc ``find`` recipes and negative items are hand-coded. French-language
items are added by scanning for sidecars tagged ``lang: "fr"``.

Run explicitly; the output is curated once by a human and committed. Never
regenerated as a side effect of an eval run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

SAMPLE_K = 10
# Negative entities — verified absent from the corpus before each generation.
NEGATIVE_INVOICE_NUMBER = "INV-99999"
NEGATIVE_VENDOR = "Globex Aerospace"


# ── helpers ─────────────────────────────────────────────────────────────────


def _to_title(name: str) -> str:
    """Strip a synthetic doc's filename extension (.json/.txt/.pdf) so the
    value matches HC's doc_title format."""
    for ext in (".json", ".txt", ".pdf"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _load_sidecar(path: Path) -> dict:
    """Read a JSON sidecar file."""
    return json.loads(path.read_text())


def _iter_sidecars(ingest_dir: Path, doctype: str) -> Iterable[tuple[str, dict]]:
    """Yield (stem, sidecar) for every ``*_<doctype>.json`` in ``ingest_dir``,
    sorted deterministically by filename."""
    for p in sorted(ingest_dir.glob(f"*_{doctype}.json")):
        yield _to_title(p.name), _load_sidecar(p)


def _lookup_item(
    *, id_: str, question: str, gold_doc: str, answer_key: str, clause_category: str
) -> dict:
    return {
        "id": id_,
        "question": question,
        "clause_category": clause_category,
        "gold_doc": gold_doc,
        "answer_key": answer_key,
        "type": "lookup",
    }


def _find_item(*, id_: str, question: str, all_matches: list[str], clause_category: str) -> dict:
    return {
        "id": id_,
        "question": question,
        "clause_category": clause_category,
        "gold_doc": "(see answer_key.all)",
        "answer_key": {
            "count": len(all_matches),
            "all": all_matches,
            "sample": all_matches[:SAMPLE_K],
        },
        "type": "find",
    }


# ── per-doc-type recipes ────────────────────────────────────────────────────


def _recipe_invoice(stem: str, sc: dict) -> list[dict]:
    inv = sc["invoice_number"]
    return [
        _lookup_item(
            id_=f"synth-invoice-total-{inv}",
            question=f"What is the total amount in USD of invoice {inv}?",
            gold_doc=stem,
            answer_key=f"${sc['total_usd']:.2f}",
            clause_category="invoice",
        ),
        _lookup_item(
            id_=f"synth-invoice-vendor-{inv}",
            question=f"Who is the vendor on invoice {inv}?",
            gold_doc=stem,
            answer_key=sc["vendor"],
            clause_category="invoice",
        ),
    ]


def _recipe_board_minutes(stem: str, sc: dict) -> list[dict]:
    return [
        _lookup_item(
            id_=f"synth-board-attendees-{sc['date']}",
            question=f"Who attended the Marbledock & Associates board meeting on {sc['date']}?",
            gold_doc=stem,
            answer_key="; ".join(sc["attendees"]),
            clause_category="board_minutes",
        ),
    ]


def _recipe_onboarding_letter(stem: str, sc: dict) -> list[dict]:
    role = sc["role"]
    return [
        _lookup_item(
            id_=f"synth-onboarding-start-{stem}",
            question=f"What is the start date listed on the {role} onboarding letter?",
            gold_doc=stem,
            answer_key=sc["start_date"],
            clause_category="onboarding_letter",
        ),
        _lookup_item(
            id_=f"synth-onboarding-mgr-{stem}",
            question=f"Who is the signing manager on the {role} onboarding letter?",
            gold_doc=stem,
            answer_key=sc["signing_manager"],
            clause_category="onboarding_letter",
        ),
    ]


def _recipe_quarterly_report(stem: str, sc: dict) -> list[dict]:
    q, y = sc["quarter"], sc["year"]
    return [
        _lookup_item(
            id_=f"synth-qreport-revenue-{q}-{y}",
            question=f"What was the revenue reported in the {q} {y} quarterly report?",
            gold_doc=stem,
            answer_key=f"${sc['revenue_usd']:,}",
            clause_category="quarterly_report",
        ),
    ]


def _recipe_vendor_contract(stem: str, sc: dict) -> list[dict]:
    vendor = sc["vendor"]
    return [
        _lookup_item(
            id_=f"synth-vcontract-law-{stem}",
            question=f"What is the governing law of the vendor contract with {vendor}?",
            gold_doc=stem,
            answer_key=sc["governing_law"],
            clause_category="vendor_contract",
        ),
        _lookup_item(
            id_=f"synth-vcontract-fee-{stem}",
            question=f"What is the monthly fee in USD on the vendor contract with {vendor}?",
            gold_doc=stem,
            answer_key=f"${sc['monthly_fee_usd']:.2f}",
            clause_category="vendor_contract",
        ),
    ]


def _recipe_internal_memo(stem: str, sc: dict) -> list[dict]:
    subj = sc["subject"]
    return [
        _lookup_item(
            id_=f"synth-memo-sender-{stem}",
            question=f"Who is the sender of the internal memo with subject \"{subj}\"?",
            gold_doc=stem,
            answer_key=sc["from"],
            clause_category="internal_memo",
        ),
    ]


def _recipe_policy_doc(stem: str, sc: dict) -> list[dict]:
    name = sc["policy_name"]
    return [
        _lookup_item(
            id_=f"synth-policy-effective-{stem}",
            question=f"When does the \"{name}\" policy take effect?",
            gold_doc=stem,
            answer_key=sc["effective_date"],
            clause_category="policy_doc",
        ),
        _lookup_item(
            id_=f"synth-policy-version-{stem}",
            question=f"What is the version number of the \"{name}\" policy?",
            gold_doc=stem,
            answer_key=str(sc["version"]),
            clause_category="policy_doc",
        ),
    ]


def _recipe_marketing_brief(stem: str, sc: dict) -> list[dict]:
    camp = sc["campaign_name"]
    return [
        _lookup_item(
            id_=f"synth-mkt-budget-{stem}",
            question=f"What is the budget in USD for the \"{camp}\" marketing campaign?",
            gold_doc=stem,
            answer_key=f"${sc['budget_usd']:,}",
            clause_category="marketing_brief",
        ),
    ]


def _recipe_employee_handbook(stem: str, sc: dict) -> list[dict]:
    year = sc["year"]
    return [
        _lookup_item(
            id_=f"synth-handbook-sections-{stem}",
            question=f"How many top-level sections are in the {year} Marbledock employee handbook?",
            gold_doc=stem,
            answer_key=str(len(sc["sections"])),
            clause_category="employee_handbook",
        ),
    ]


RECIPES: dict[str, callable] = {
    "invoice": _recipe_invoice,
    "board_minutes": _recipe_board_minutes,
    "onboarding_letter": _recipe_onboarding_letter,
    "quarterly_report": _recipe_quarterly_report,
    "vendor_contract": _recipe_vendor_contract,
    "internal_memo": _recipe_internal_memo,
    "policy_doc": _recipe_policy_doc,
    "marketing_brief": _recipe_marketing_brief,
    "employee_handbook": _recipe_employee_handbook,
}


# ── cross-doc finds + negatives + FR ────────────────────────────────────────


def _find_invoices_over_5000(ingest_dir: Path) -> dict:
    matches = [
        stem for stem, sc in _iter_sidecars(ingest_dir, "invoice")
        if float(sc.get("total_usd", 0)) > 5000
    ]
    return _find_item(
        id_="synth-find-invoices-over-5000",
        question="Find all invoices with a total amount over $5,000 USD.",
        all_matches=sorted(matches),
        clause_category="cross-doc",
    )


def _find_policies_effective_in_q4_2025(ingest_dir: Path) -> dict:
    matches = [
        stem for stem, sc in _iter_sidecars(ingest_dir, "policy_doc")
        if str(sc.get("effective_date", "")).startswith(("2025-10", "2025-11", "2025-12"))
    ]
    return _find_item(
        id_="synth-find-policies-q4-2025",
        question="Find Marbledock policies that take effect in Q4 2025 (October, November, or December 2025).",
        all_matches=sorted(matches),
        clause_category="cross-doc",
    )


def _neg_invoice_number(ingest_dir: Path) -> dict:
    """A lookup with an invoice_number that doesn't exist in the corpus."""
    for _, sc in _iter_sidecars(ingest_dir, "invoice"):
        if sc.get("invoice_number") == NEGATIVE_INVOICE_NUMBER:
            raise RuntimeError(
                f"negative invoice_number {NEGATIVE_INVOICE_NUMBER!r} unexpectedly matches "
                f"a real invoice in the corpus; pick a different absent number"
            )
    return {
        "id": f"synth-neg-invoice-{NEGATIVE_INVOICE_NUMBER.split('-')[-1]}",
        "question": f"What is the total amount of invoice {NEGATIVE_INVOICE_NUMBER}?",
        "clause_category": "invoice",
        "gold_doc": "(none expected)",
        "answer_key": None,
        "type": "negative",
    }


def _find_neg_vendor(ingest_dir: Path) -> dict:
    """A find with a vendor that doesn't exist anywhere in the corpus."""
    for p in sorted(ingest_dir.glob("*.json")):
        if NEGATIVE_VENDOR.lower() in p.read_text().lower():
            raise RuntimeError(
                f"negative vendor {NEGATIVE_VENDOR!r} unexpectedly matches "
                f"content in {p.name}; pick a different absent vendor"
            )
    slug = NEGATIVE_VENDOR.lower().replace(" ", "-")
    return _find_item(
        id_=f"synth-find-neg-vendor-{slug}",
        question=f"Find vendor contracts where the vendor is {NEGATIVE_VENDOR}.",
        all_matches=[],
        clause_category="cross-doc",
    )


def _emit_fr_items(ingest_dir: Path) -> list[dict]:
    """Emit ≤2 French-language items derived from sidecars tagged ``lang: "fr"``.
    Scans board_minutes and internal_memo (the types that carry an explicit
    ``lang`` field in the corpus); falls back silently if none are present.
    """
    items: list[dict] = []
    for doctype in ("board_minutes", "internal_memo"):
        for stem, sc in _iter_sidecars(ingest_dir, doctype):
            if sc.get("lang") != "fr":
                continue
            if doctype == "board_minutes":
                items.append(
                    _lookup_item(
                        id_=f"synth-fr-board-attendees-{sc['date']}",
                        question=f"Qui a assisté à la réunion du conseil de Marbledock & Associates le {sc['date']}?",
                        gold_doc=stem,
                        answer_key="; ".join(sc["attendees"]),
                        clause_category="board_minutes",
                    )
                )
            else:  # internal_memo
                items.append(
                    _lookup_item(
                        id_=f"synth-fr-memo-sender-{stem}",
                        question=f"Qui est l'expéditeur du mémo interne dont le sujet est « {sc['subject']} »?",
                        gold_doc=stem,
                        answer_key=sc["from"],
                        clause_category="internal_memo",
                    )
                )
            if len(items) >= 2:
                return items
    return items


# ── orchestrator ────────────────────────────────────────────────────────────


def generate(ingest_dir: Path, out_path: Path, *, per_type: int = 2) -> int:
    """Emit the Synthetic ground-truth YAML. Returns the number of items written.

    For each doc-type, applies the per-type recipe to the first ``per_type``
    sidecars (sorted by filename). Adds cross-doc finds, negatives (validated
    against the corpus), and up to 2 French-language items.
    """
    items: list[dict] = []

    # Per-doc-type lookups.
    for doctype, recipe in RECIPES.items():
        for stem, sc in list(_iter_sidecars(ingest_dir, doctype))[:per_type]:
            items.extend(recipe(stem, sc))

    # Cross-doc finds.
    items.append(_find_invoices_over_5000(ingest_dir))
    items.append(_find_policies_effective_in_q4_2025(ingest_dir))

    # Negatives (validated against the corpus — raises if either accidentally matches).
    items.append(_neg_invoice_number(ingest_dir))
    items.append(_find_neg_vendor(ingest_dir))

    # French-language items.
    items.extend(_emit_fr_items(ingest_dir))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump({"corpus": "synthetic", "items": items}, sort_keys=False))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the Synthetic answer-eval ground-truth set.")
    ap.add_argument("--ingest-dir", type=Path, required=True, help="Synthetic ingest dir (*.json + *.txt)")
    ap.add_argument("--out", type=Path, required=True, help="output synthetic.yaml")
    ap.add_argument("--per-type", type=int, default=2, help="docs sampled per doc-type")
    a = ap.parse_args(argv)
    n = generate(ingest_dir=a.ingest_dir, out_path=a.out, per_type=a.per_type)
    print(f"wrote {n} ground-truth items -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_generate_synthetic.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/groundtruth/generate_synthetic.py scripts/test_corpora/tests/test_generate_synthetic.py
uv run --project scripts/test_corpora ruff format scripts/test_corpora/groundtruth/generate_synthetic.py scripts/test_corpora/tests/test_generate_synthetic.py
git add scripts/test_corpora/groundtruth/generate_synthetic.py scripts/test_corpora/tests/test_generate_synthetic.py
git commit -m "feat(eval): Synthetic ground-truth generator (sidecar-driven, 9 doc-types)"
```

Append this trailer to the commit message (blank line before it):
`Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

## Task 2: Generate & curate the frozen `synthetic.yaml`

Operational — run the generator against the live Synthetic corpus, eyeball the output, commit the frozen artifact. Mirrors PR-A's curation discipline.

- [ ] **Step 1: Run the generator against the live corpus**

```bash
WD="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
uv run --project scripts/test_corpora python -m scripts.test_corpora.groundtruth.generate_synthetic \
  --ingest-dir "$WD/synthetic/ingest" \
  --out scripts/test_corpora/groundtruth/synthetic.yaml
```

Expected: `wrote N ground-truth items -> scripts/test_corpora/groundtruth/synthetic.yaml` where N ≈ 18–24 (9 doc-types × 1–2 items + 2 cross-doc + 2 negatives + 0–2 FR).

If a negative item raises `RuntimeError: negative ... unexpectedly matches`, pick a replacement (e.g., a different fictional vendor name or invoice number guaranteed to be absent — `Globex Maritime`, `INV-00000`) and update the `NEGATIVE_INVOICE_NUMBER` / `NEGATIVE_VENDOR` constants in `generate_synthetic.py`, then rerun.

- [ ] **Step 2: Human curation pass**

Open `scripts/test_corpora/groundtruth/synthetic.yaml`. For each item confirm:

- `answer_key` is a real, sensible value (no `null` for `lookup` items, no `(see answer_key.all)` literal text leaking into a value, totals formatted as currency).
- `gold_doc` for `lookup` items is a real stem.
- Question text reads naturally — especially the FR items (correct French; the judge will mark wrong-language as a real mismatch).
- The two `find` items have non-empty `all` lists (otherwise the question is mis-targeted; either tighten the filter or pick a different find).
- The two negatives carry the right shapes (`lookup`-negative has `answer_key: null`, `find`-negative has `count: 0`).

If FR items count = 0, check whether the corpus has any `lang: "fr"` sidecars (`grep -l '"lang": "fr"' "$WD/synthetic/ingest"/*.json`). If none, that's a real corpus property — the eval just runs without an FR signal; note it in the run summary.

If anything looks garbled, edit the YAML by hand — the committed YAML is the source of truth; the generator is a one-shot helper.

- [ ] **Step 3: Commit the frozen set**

```bash
git add scripts/test_corpora/groundtruth/synthetic.yaml
git commit -m "feat(eval): frozen Synthetic ground-truth set (~20 items across 9 doc-types)"
```

Append the Co-Authored-By trailer.

---

## Task 3: De-risk + first real Synthetic run

Operational. Spot-check the generator's output against HC's index, run the eval end-to-end, post results to the PR.

- [ ] **Step 1: Spot-check 1 item against HC** — manual `/api/search` call to confirm a gold doc's title is indexed.

```bash
HC_API_KEY="<synthetic-scoped key>"
API_BASE="http://localhost:8100"

# Pick the first invoice's stem from the generated yaml; search HC for that stem.
GOLD=$(python3 -c "
import yaml
d = yaml.safe_load(open('scripts/test_corpora/groundtruth/synthetic.yaml'))
print(next(i['gold_doc'] for i in d['items'] if i['clause_category']=='invoice'))
")
echo "spot-checking gold doc: $GOLD"
curl -s -H "Authorization: Bearer $HC_API_KEY" \
  -X POST "$API_BASE/api/search" -H "Content-Type: application/json" \
  -d "{\"query\": \"$GOLD\", \"k\": 5}" | python3 -m json.tool | head -30
```

Expected: HC returns the doc whose title matches `$GOLD` among the top results.

- [ ] **Step 2: Full run for all ~20 items**

```bash
WD="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
HC_API_KEY="<synthetic-scoped>" ANTHROPIC_API_KEY="<...>" \
  uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id answer-eval --mode answer-eval \
  --corpora synthetic --models claude-sonnet-4-6 --label synthetic-phase2b \
  --workdir "$WD" --api-base http://localhost:8100
```

Expected: completes with `OVERALL n=… correctness=… groundedness=… completeness=…`; per-label report at `<WD>/answer-eval/reports/synthetic-phase2b/summary.json`.

- [ ] **Step 3: Sanity-check the report and post results to the PR**

```bash
cat "$WD/answer-eval/reports/synthetic-phase2b/summary.json"
```

Confirm:
- `overall.n` matches the YAML's item count.
- `by_type` includes `lookup`, `find`, and `negative` buckets (depending on the curated set).
- Spot-check a couple of `detail.json` items for `find` to confirm `source.completeness == "deterministic"`.

Post a comment to the PR with the headline numbers + a couple of per-item observations, mirroring PR-A's validation comment.

---

## Self-Review

**1. Spec coverage:**
- §1 Goal → all tasks.
- §2 Scope (Synthetic, lookup/find/negative, Sonnet 4.6) → Tasks 1, 2, 3.
- §3 Why Synthetic + sidecar ground truth → Task 1 (`_load_sidecar`, recipes).
- §4 Architecture (additive only) → Task 1 (one new generator); judge/runner/sweep untouched.
- §5 Generator structure (per-doc-type recipes + dispatch) → Task 1 (`RECIPES` dict).
- §6 Bilingual coverage (2–3 FR items, fallback) → Task 1 (`_emit_fr_items`), Task 2 Step 2 (fallback diagnosis).
- §7 Item composition (~20 items) → Task 1 (orchestrator sums the parts), validated by the end-to-end test.
- §8 Ground-truth shape (`GTItem` unchanged, `gold_doc` is stem) → Task 1 (`_to_title`, `_lookup_item`, `_find_item`).
- §9 Harness integration (zero CLI changes) → Task 3 (uses the existing sweep CLI).
- §10 Testing → Task 1's 10 tests + Task 3's operational sanity check.
- §11 Out of scope (aggregations / multi-model / prompt-tuning) — respected; not implemented.
- §12 Open questions for the plan — recipe content settled with verbatim code; second cross-doc find chosen (`_find_policies_effective_in_q4_2025`); FR-picking strategy settled (scan `board_minutes` + `internal_memo` for `lang: "fr"`, fall back silently if none).

**2. Placeholder scan:** Credential placeholders `<synthetic-scoped>` and `<...>` in Task 3 commands are operator-substitution slots (mirror PR-A's plan). No "TBD" / "TODO" / "implement later" anywhere. The Task 2 Step 2 fallback for FR-items-not-present is explicitly documented as "expected behavior; note it in the run summary," not a deferred decision.

**3. Type consistency:**
- `_to_title(name: str) -> str`, `_load_sidecar(path: Path) -> dict`, `_iter_sidecars(ingest_dir: Path, doctype: str) -> Iterable[tuple[str, dict]]`, `_lookup_item(*, id_, question, gold_doc, answer_key, clause_category) -> dict`, `_find_item(*, id_, question, all_matches, clause_category) -> dict`: consistent definitions in Step 3, used identically across the 9 recipes and the cross-doc / negative helpers.
- Item dicts match the `GTItem` schema validated by `load_groundtruth` (from PR-A): `id`, `question`, `clause_category`, `gold_doc`, `answer_key`, `type`. The `find` `answer_key` is `{count, all, sample}` per PR-A's validation; `lookup` `answer_key` is `str`; `negative` `answer_key` is `None`.
- `generate(ingest_dir: Path, out_path: Path, *, per_type: int = 2) -> int`: signature consistent in Task 1 Step 3, Task 1 Step 4 tests, and Task 2 Step 1 CLI.

No inconsistencies.
