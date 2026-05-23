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


def _lookup_item(*, id_: str, question: str, gold_doc: str, answer_key: str, clause_category: str) -> dict:
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
            id_=f"synth-invoice-total-{stem}",
            question=f"What is the total amount in USD of invoice {inv}?",
            gold_doc=stem,
            answer_key=f"${sc['total_usd']:,.2f}",
            clause_category="invoice",
        ),
        _lookup_item(
            id_=f"synth-invoice-vendor-{stem}",
            question=f"Who is the vendor on invoice {inv}?",
            gold_doc=stem,
            answer_key=sc["vendor"],
            clause_category="invoice",
        ),
    ]


def _recipe_board_minutes(stem: str, sc: dict) -> list[dict]:
    return [
        _lookup_item(
            id_=f"synth-board-attendees-{stem}",
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
            id_=f"synth-qreport-revenue-{stem}",
            question=f"What was the revenue reported in the {q} {y} quarterly report?",
            gold_doc=stem,
            answer_key=f"${sc['revenue_usd']:,.2f}",
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
            answer_key=f"${sc['monthly_fee_usd']:,.2f}",
            clause_category="vendor_contract",
        ),
    ]


def _recipe_internal_memo(stem: str, sc: dict) -> list[dict]:
    subj = sc["subject"]
    return [
        _lookup_item(
            id_=f"synth-memo-sender-{stem}",
            question=f'Who is the sender of the internal memo with subject "{subj}"?',
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
            question=f'When does the "{name}" policy take effect?',
            gold_doc=stem,
            answer_key=sc["effective_date"],
            clause_category="policy_doc",
        ),
        _lookup_item(
            id_=f"synth-policy-version-{stem}",
            question=f'What is the version number of the "{name}" policy?',
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
            question=f'What is the budget in USD for the "{camp}" marketing campaign?',
            gold_doc=stem,
            answer_key=f"${sc['budget_usd']:,.2f}",
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
    matches = [stem for stem, sc in _iter_sidecars(ingest_dir, "invoice") if float(sc.get("total_usd", 0)) > 5000]
    return _find_item(
        id_="synth-find-invoices-over-5000",
        question="Find all invoices with a total amount over $5,000 USD.",
        all_matches=sorted(matches),
        clause_category="cross-doc",
    )


def _find_policies_effective_in_q4_2025(ingest_dir: Path) -> dict:
    matches = [
        stem
        for stem, sc in _iter_sidecars(ingest_dir, "policy_doc")
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

    Scans board_minutes first, then internal_memo (the types that carry an
    explicit ``lang`` field in the corpus); board_minutes items are
    prioritised — internal_memo items are only emitted if fewer than 2
    board_minutes FR items exist. Falls back silently if no FR sidecars are
    present at all.
    """
    items: list[dict] = []
    for doctype in ("board_minutes", "internal_memo"):
        for stem, sc in _iter_sidecars(ingest_dir, doctype):
            if sc.get("lang") != "fr":
                continue
            if doctype == "board_minutes":
                items.append(
                    _lookup_item(
                        id_=f"synth-fr-board-attendees-{stem}",
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

    # Guard against silent duplicate ids — recipe ids are derived from sidecar
    # fields (invoice_number, date, role, etc.), so two sidecars sharing such a
    # value would collide. YAML allows duplicate keys at sequence-of-mappings
    # level; downstream loaders may or may not dedup. Fail loud at generation.
    ids = [i["id"] for i in items]
    if len(ids) != len(set(ids)):
        from collections import Counter

        dups = sorted(k for k, v in Counter(ids).items() if v > 1)
        raise RuntimeError(f"duplicate item ids in generated set: {dups}")

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
