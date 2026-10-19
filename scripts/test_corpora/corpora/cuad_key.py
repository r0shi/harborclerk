"""Questions with an answer key, written from CUAD's own annotations.

    uv --project scripts/test_corpora run python -m scripts.test_corpora.corpora.cuad_key --workdir "$WORKDIR"

reads `<workdir>/cuad/extracted/CUAD_v1/master_clauses.csv` and the contracts in `<workdir>/cuad/ingest`, and
writes `questions/keyed/cuad.yaml` (the sweep's format, `ask` only) and `questions/keyed/cuad.key.json` (what
`runner/answer_key.py` scores against). Both are committed: the ingest sample is the first 80 contracts by name,
so the key is the same on every machine, and an experiment should cite a file, not a regeneration.

Only questions the annotations answer without interpretation are asked: a contract's governing law, a date, and
"which contracts have clause X" for clause types labelled Yes or No. A contract is asked about only if its filer
has one contract in the sample, so that its name in a question is unambiguous.

A contract is joined to its annotation row by its file name, normalised (`normalize`). `master_clauses.csv` truncates
some names (`HarpoonTherapeuticsInc_20200312_10-K_EX-10.18_12051356_EX-10.18_Development Agreement` for the PDF that
goes on to `_Option Agreement`; #815), so a contract with no exact row is matched by `annotation_rows`'s two inexact
rules, each logged, and refused, also logged, whenever it could pair one row with two contracts or one contract with
two rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml

from scripts.test_corpora.runner.answer_key import company, normalize, score

log = logging.getLogger(__name__)

OUT_DIR = Path(__file__).resolve().parent.parent / "questions" / "keyed"

# What a CUAD file name carries before its exhibit's description: filer, filing date, form, exhibit number and EDGAR
# accession number (`HarpoonTherapeuticsInc_20200312_10-K_EX-10.18_12051356`). They narrow the candidates; they do
# not identify a contract. In the 510-PDF release 11 groups are shared by two to four PDFs of one split exhibit, and
# 329 names are not in this shape at all (`KALLOINC_11_03_2011-EX-10.1-STRATEGIC ALLIANCE AGREEMENT`) and have none.
# A match is unique only because `annotation_rows` refuses every row another contract could also claim: loosen
# those refusals and the identifiers alone will pair a row with the wrong half of a split exhibit.
_IDENTIFIERS = re.compile(r"[^_]+_\d{8}_[^_]+_[^_]+_\d+(?=_)")

# (clause column, how the question says it). Chosen for a positive count in the sample that is neither a
# handful nor most of it, so that precision and recall both have room to move.
LIST_CLAUSES = (
    ("No-Solicit Of Customers", "a clause prohibiting the solicitation of the other party's customers"),
    ("Non-Disparagement", "a non-disparagement clause"),
    ("Irrevocable Or Perpetual License", "an irrevocable or perpetual license grant"),
    ("Rofr/Rofo/Rofn", "a right of first refusal, first offer or first negotiation"),
    ("Insurance", "a requirement that a party maintain insurance"),
)
LIST_GOVERNING_LAW = "Delaware"
N_GOVERNING_LAW, N_AGREEMENT_DATE, N_EXPIRATION_DATE = 8, 3, 3


def display_name(title: str) -> str:
    """`StaarSurgicalCompany_..._Distributor Agreement` -> `Staar Surgical Company Distributor Agreement`."""
    filer = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", title.split("_")[0])
    kind = title.split("_")[-1].strip()
    return f"{filer} {kind}"


def _text(pdf: Path) -> str:
    import pypdfium2 as pdfium  # a harness dependency already; imported here so that scoring never needs it

    return "\n".join(page.get_textpage().get_text_range() for page in pdfium.PdfDocument(str(pdf)))


def _iso(mdy: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", mdy.strip())
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 100:
        year += 1900 if year >= 50 else 2000  # the corpus runs from the 1990s to 2020
    return f"{year:04d}-{month:02d}-{day:02d}"


def identifiers(name: str) -> str | None:
    """The normalised leading identifiers of a CUAD file name, up to and including its accession number, or None
    for a name not in that shape (`_IDENTIFIERS`)."""
    m = _IDENTIFIERS.match(name)
    return normalize(m.group(0)) if m else None


def _same_identifiers(row: str, name: str) -> bool:
    return identifiers(row) is not None and identifiers(row) == identifiers(name)


def _truncated(row: str, name: str) -> bool:
    """The row's name is the beginning of the contract's: their longest common prefix is the whole row name."""
    return normalize(name).startswith(normalize(row))


_RULES: tuple[tuple[str, Callable[[str, str], bool]], ...] = (
    ("identifiers to the accession number", _same_identifiers),
    ("the row's name is a prefix of the title", _truncated),
)


def annotation_rows(titles: list[str], rows: list[dict], release: Iterable[str] = ()) -> dict[str, dict]:
    """title -> its annotation row, for each title that has one. A title whose normalised name is a row's is that
    row's. Otherwise the rules in `_RULES` are tried in order, and the first that finds any row for the title
    decides, matching only if it finds exactly one row, that row is no contract's by name, and no other contract
    fits that row by any rule; anything else is refused and logged, and the title stays outside the key.
    "Contract" is any ingested title or any PDF of the CUAD release (`release`, the stems of the files the sample
    was drawn from), since a row that could be a contract outside the sample's is not this one's. So a row two
    titles reach, by the same rule or by different ones, goes to neither. Every inexact match is logged."""
    by_stem = {Path(r["Filename"]).stem: r for r in rows}
    exact = {normalize(stem): r for stem, r in by_stem.items()}
    names = sorted(set(titles) | set(release))
    taken = {normalize(n) for n in names}  # a row whose name is a contract's is that contract's
    out: dict[str, dict] = {}
    for title in titles:
        if normalize(title) in exact:
            out[title] = exact[normalize(title)]
            continue
        for rule_name, rule in _RULES:
            found = [stem for stem in by_stem if rule(stem, title)]
            if not found:
                continue
            row = found[0]
            if len(found) > 1:
                log.warning("cuad key: %r not matched (%s): rows %s", title, rule_name, found)
            elif normalize(row) in taken:
                log.warning("cuad key: %r not matched (%s): row %r is another contract's", title, rule_name, row)
            elif (claimants := [n for n in names if any(fits(row, n) for _, fits in _RULES)]) != [title]:
                log.warning("cuad key: %r not matched (%s): row %r fits %s", title, rule_name, row, claimants)
            else:
                log.info("cuad key: %r matched inexactly (%s) to row %r", title, rule_name, row)
                out[title] = by_stem[row]
            break
        else:
            log.warning("cuad key: %r has no annotation row", title)
    return out


def build(workdir: Path) -> tuple[dict, dict]:
    base = workdir / "cuad"
    with open(
        base / "extracted" / "CUAD_v1" / "master_clauses.csv", newline="", encoding="utf-8", errors="replace"
    ) as f:
        annotated = list(csv.DictReader(f))
    titles = sorted(p.stem for p in (base / "ingest").glob("*.pdf"))
    # Every contract of the release, `.pdf` or `.PDF` (the sample took only the first; the release has both).
    release = sorted(p.stem for p in (base / "extracted").rglob("*") if p.suffix.lower() == ".pdf")
    rows = annotation_rows(titles, annotated, release)
    universe = sorted(rows)
    filers: dict[str, int] = {}
    for t in universe:
        filers[company(t)] = filers.get(company(t), 0) + 1
    askable = [t for t in universe if filers[company(t)] == 1]

    questions, key = [], {}

    def add(text: str, entry: dict, about: str) -> bool:
        # A single-contract key must be in that contract's own words. The annotators also record dates they
        # worked out ("one year from the effective date" as 10/1/02); a model that answers in the contract's
        # terms would be marked wrong for it, and that is an interpretation, not a label.
        if "contract" in entry and score(entry, _text(base / "ingest" / f"{entry['contract']}.pdf"))["score"] < 1.0:
            return False
        qid = f"cuad-key-{len(questions) + 1}"
        questions.append({"id": qid, "text": text, "notes": about})
        key[qid] = entry
        return True

    seen_law: set[str] = set()
    for t in askable:  # one contract per jurisdiction, so the eight are not all New York
        law = (rows[t].get("Governing Law-Answer") or "").strip()
        if not law or law == "[]" or ";" in law or law in seen_law or len(seen_law) >= N_GOVERNING_LAW:
            continue
        entry = {"kind": "fact", "all_of": [[law]], "contract": t}
        if add(f"What is the governing law of the {display_name(t)}?", entry, f"Governing Law-Answer = {law!r}"):
            seen_law.add(law)
    for column, how, count in (
        ("Agreement Date", "What is the agreement date of the {}?", N_AGREEMENT_DATE),
        ("Expiration Date", "On what date does the initial term of the {} expire?", N_EXPIRATION_DATE),
    ):
        asked = {e.get("contract") for e in key.values()}
        picked = 0
        for t in askable:
            iso = _iso(rows[t].get(f"{column}-Answer") or "")
            if not iso or t in asked or picked >= count:
                continue
            entry = {"kind": "date", "date": iso, "contract": t}
            picked += add(how.format(display_name(t)), entry, f"{column}-Answer = {iso}")
    for column, phrase in LIST_CLAUSES:
        positives = [t for t in universe if (rows[t].get(f"{column}-Answer") or "").strip() == "Yes"]
        add(
            f"Which contracts contain {phrase}? List every one you can find.",
            {"kind": "list", "positives": positives, "universe": universe},
            f"{column}-Answer = Yes for {len(positives)} of {len(universe)} annotated contracts in the sample",
        )
    positives = [t for t in universe if (rows[t].get("Governing Law-Answer") or "").strip() == LIST_GOVERNING_LAW]
    add(
        f"Which contracts are governed by {LIST_GOVERNING_LAW} law? List every one you can find.",
        {"kind": "list", "positives": positives, "universe": universe},
        f"Governing Law-Answer = {LIST_GOVERNING_LAW!r} for {len(positives)} of {len(universe)}",
    )
    return {"research": [], "ask": questions}, key


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--workdir", required=True, type=Path)
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    questions, key = build(args.workdir.expanduser())
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "cuad.yaml").write_text(yaml.safe_dump(questions, sort_keys=False, allow_unicode=True, width=110))
    (args.out_dir / "cuad.key.json").write_text(json.dumps(key, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(questions['ask'])} questions -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
