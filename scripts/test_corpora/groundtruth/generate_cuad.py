# scripts/test_corpora/groundtruth/generate_cuad.py
"""Generate the CUAD answer-eval ground-truth set from master_clauses.csv.

CUAD ships expert clause labels (Atticus Project). master_clauses.csv has, per
contract, a `<Category>` column and a `<Category>-Answer` column; the -Answer
cell is a Python-list-repr string of the extracted clause text(s), or "[]" when
the clause is absent. We turn a fixed set of (contract, category) pairs into
ground-truth Q&A: lookups (clause present) and negatives (clause absent).

Run explicitly; the output is curated once by a human and committed. It is
never regenerated as a side effect of an eval run.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

# Clause category -> question template. {name} is filled with the contract's
# filename stem. Categories chosen for crisp, checkable answers.
CATEGORIES: dict[str, str] = {
    "Governing Law": "What is the governing law of the {name} agreement?",
    "Parties": "Who are the parties to the {name} agreement?",
    "Agreement Date": "What is the agreement date of the {name} agreement?",
    "Expiration Date": "What is the expiration date of the {name} agreement?",
    "Cap On Liability": "What is the cap on liability in the {name} agreement?",
    "Exclusivity": "What exclusivity clause does the {name} agreement contain?",
    "Most Favored Nation": "Does the {name} agreement contain a most-favored-nation clause?",
    "Non-Compete": "What non-compete clause does the {name} agreement contain?",
}


def _parse_answer(cell: str | None) -> str | None:
    """Rough extraction from a master_clauses.csv -Answer cell — a
    Python-list-repr string like "['Delaware']", or "[]" when the clause is
    absent. Returns the answer text, or None when absent. Deliberately crude
    (no code evaluation): the generated set is finalized by a human curation
    pass before being committed, so first-pass roughness is acceptable.
    """
    cell = (cell or "").strip()
    if cell in ("", "[]"):
        return None
    inner = cell.strip("[]").strip()
    # First quoted element, surrounding quote stripped.
    first = inner.split("',")[0].split('",')[0].strip()
    return first.strip("'\"").strip() or None


def generate(master_csv: Path, ingest_dir: Path, out_path: Path, per_category: int = 2) -> int:
    """Emit the ground-truth YAML. Returns the number of items written.

    Selects up to `per_category` lookup items per category, plus one negative
    per category where one exists (a contract where CUAD labeled the clause
    absent). Contracts not present in `ingest_dir` (i.e. not in the sampled set
    HC actually holds) are skipped. Deterministic by sorted filename.
    """
    sampled = {p.stem for p in sorted(ingest_dir.glob("*.pdf"))}
    rows = sorted(csv.DictReader(master_csv.open()), key=lambda r: r.get("Filename", ""))

    items: list[dict] = []
    for category, template in CATEGORIES.items():
        answer_col = f"{category}-Answer"
        lookups: list[dict] = []
        negatives: list[dict] = []
        for row in rows:
            filename = (row.get("Filename") or "").strip()
            stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
            if stem not in sampled:
                continue
            answer = _parse_answer(row.get(answer_col))
            if answer is not None and len(lookups) < per_category:
                lookups.append(
                    {
                        "id": f"cuad-gt-{category.lower().replace(' ', '-')}-{len(lookups) + 1}",
                        "question": template.format(name=stem),
                        "clause_category": category,
                        "gold_doc": stem,
                        "answer_key": answer,
                        "type": "lookup",
                    }
                )
            elif answer is None and len(negatives) < 1:
                negatives.append(
                    {
                        "id": f"cuad-gt-{category.lower().replace(' ', '-')}-neg",
                        "question": template.format(name=stem),
                        "clause_category": category,
                        "gold_doc": stem,
                        "answer_key": None,
                        "type": "negative",
                    }
                )
        items.extend(lookups + negatives)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump({"corpus": "cuad", "items": items}, sort_keys=False))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the CUAD answer-eval ground-truth set.")
    ap.add_argument("--master-csv", type=Path, required=True, help="CUAD master_clauses.csv")
    ap.add_argument("--ingest-dir", type=Path, required=True, help="CUAD ingest dir (sampled PDFs)")
    ap.add_argument("--out", type=Path, required=True, help="output cuad.yaml")
    ap.add_argument("--per-category", type=int, default=2)
    a = ap.parse_args(argv)
    n = generate(master_csv=a.master_csv, ingest_dir=a.ingest_dir, out_path=a.out, per_category=a.per_category)
    print(f"wrote {n} ground-truth items -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
