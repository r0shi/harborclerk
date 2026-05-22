# scripts/test_corpora/tests/test_generate_cuad.py
import csv
from pathlib import Path

import yaml

from scripts.test_corpora.groundtruth.generate_cuad import generate


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def test_generate_emits_lookup_and_negative_items(tmp_path: Path):
    master = tmp_path / "master_clauses.csv"
    _write_csv(
        master,
        [
            {
                "Filename": "AcmeCo_Distributor Agreement.pdf",
                "Governing Law-Answer": "['Delaware']",
                "Most Favored Nation-Answer": "[]",
            },
            {
                "Filename": "BetaCo_License Agreement.pdf",
                "Governing Law-Answer": "['New York']",
                "Most Favored Nation-Answer": "['Section 4.2 grants MFN pricing']",
            },
        ],
    )
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    (ingest / "AcmeCo_Distributor Agreement.pdf").write_text("x")
    (ingest / "BetaCo_License Agreement.pdf").write_text("x")
    out = tmp_path / "cuad.yaml"

    n = generate(master_csv=master, ingest_dir=ingest, out_path=out, per_category=2)

    assert n >= 3
    data = yaml.safe_load(out.read_text())
    assert data["corpus"] == "cuad"
    by_type = {}
    for item in data["items"]:
        by_type.setdefault(item["type"], []).append(item)
        assert item["gold_doc"]  # filename stem, no .pdf
        assert not item["gold_doc"].endswith(".pdf")
    assert "lookup" in by_type and "negative" in by_type
    neg = by_type["negative"][0]
    assert neg["answer_key"] is None  # CUAD labeled the clause absent
    law = next(i for i in by_type["lookup"] if i["clause_category"] == "Governing Law")
    assert law["answer_key"] == "Delaware"
    assert law["gold_doc"] in {"AcmeCo_Distributor Agreement", "BetaCo_License Agreement"}


def test_generate_skips_contracts_not_in_ingest(tmp_path: Path):
    master = tmp_path / "master_clauses.csv"
    _write_csv(
        master,
        [
            {"Filename": "NotSampled_Agreement.pdf", "Governing Law-Answer": "['Texas']"},
        ],
    )
    ingest = tmp_path / "ingest"
    ingest.mkdir()  # empty -- NotSampled is not present
    out = tmp_path / "cuad.yaml"

    n = generate(master_csv=master, ingest_dir=ingest, out_path=out, per_category=2)

    assert n == 0
    assert yaml.safe_load(out.read_text())["items"] == []
