# scripts/test_corpora/groundtruth/generate_enron.py
"""Generate the Enron answer-eval ground-truth set by grepping `.eml` files.

Per the answer-eval phase-2a design (Enron), there is no expert-labeled set
analogous to CUAD's master_clauses.csv. The clean alternative — and what makes
coverage (recall) measurable — is to derive truth by directly querying the
raw filesystem: `grep`-style per-term searches plus a small amount of email
header parsing.

Each recipe function emits one item. The orchestrator collects all items, then
verifies that the chosen negative search terms have zero hits before emitting
their negative items (refuses to write the YAML otherwise — that would mask a
corpus shift). Run explicitly; the output is curated once by a human and
committed. Never regenerated as a side effect of an eval run.
"""

from __future__ import annotations

import argparse
import email
import email.utils
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

SENTINEL_DATE_CUTOFF = datetime(1995, 1, 1, tzinfo=UTC)
SAMPLE_K = 10
SKILLING_RESIGN_DATE = datetime(2001, 8, 14, tzinfo=UTC)
SKILLING_ADDRS = ("jeff.skilling", "jskilling", "skilling@enron")
NEGATIVE_TERMS: list[tuple[str, str]] = [
    ("cryptocurrency", "cryptocurrency"),
    ("bitcoin", "Bitcoin"),
    ("spacex", "SpaceX"),
]


# ── helpers ─────────────────────────────────────────────────────────────────


def _grep(ingest_dir: Path, pattern: str) -> list[str]:
    """Return sorted list of .eml filenames matching `pattern` (case-insensitive,
    extended-regex). Uses GNU/BSD `grep -lirE` — list filenames, recursive."""
    result = subprocess.run(
        ["grep", "-l", "-i", "-r", "-E", pattern, str(ingest_dir)],
        capture_output=True,
        text=True,
        check=False,  # grep exits 1 on no matches, which is fine
    )
    files = [Path(p).name for p in result.stdout.strip().splitlines() if p]
    return sorted(files)


def _parse_email_date(path: Path) -> datetime | None:
    """Parse an .eml file's Date header; return None if missing, malformed, or
    a pre-1995 sentinel (PST extractions stub Dates as 1980-01-01 etc.)."""
    try:
        msg = email.message_from_string(path.read_text(errors="replace"))
    except Exception:
        return None
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # Compare in UTC to avoid tzinfo mismatches.
    dt_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    if dt_utc < SENTINEL_DATE_CUTOFF:
        return None
    return dt


def _find_item(id_: str, question: str, *, all_matches: list[str]) -> dict:
    """Build a `find`-typed item with the {count, all, sample} answer_key shape."""
    return {
        "id": id_,
        "question": question,
        "clause_category": "n/a",
        "gold_doc": "(see answer_key.all)",
        "answer_key": {
            "count": len(all_matches),
            "all": all_matches,
            "sample": all_matches[:SAMPLE_K],
        },
        "type": "find",
    }


# ── recipes (one per ground-truth item) ─────────────────────────────────────


def _recipe_raptor(ingest: Path) -> dict:
    return _find_item("enron-find-raptor", "Find emails about Raptor.", all_matches=_grep(ingest, "raptor"))


def _recipe_ljm(ingest: Path) -> dict:
    return _find_item("enron-find-ljm", "Find emails about LJM.", all_matches=_grep(ingest, "ljm"))


def _recipe_offbalance(ingest: Path) -> dict:
    return _find_item(
        "enron-find-offbalancesheet",
        "Find emails containing 'off-balance-sheet'.",
        all_matches=_grep(ingest, "off.balance.sheet"),
    )


def _recipe_andersen(ingest: Path) -> dict:
    return _find_item(
        "enron-find-arthurandersen",
        "Find emails mentioning Arthur Andersen.",
        all_matches=_grep(ingest, "arthur.andersen"),
    )


def _recipe_ferc(ingest: Path) -> dict:
    return _find_item("enron-find-ferc", "Find emails about FERC.", all_matches=_grep(ingest, "\\bferc\\b"))


def _recipe_lay_forwarded_2001(ingest: Path) -> dict:
    """List emails forwarded by Lay during 2001 — heuristic over the lay-k
    folder: 2001 in Date header + Fw/Fwd in Subject."""
    matches: list[str] = []
    for p in sorted(ingest.glob("lay-k*.eml")):
        try:
            msg = email.message_from_string(p.read_text(errors="replace"))
        except Exception:
            continue
        subj = (msg.get("Subject") or "").lower()
        date = msg.get("Date") or ""
        if "2001" not in date:
            continue
        if not any(tag in subj for tag in ("fw:", "fwd:", "fw ", "fwd ")):
            continue
        matches.append(p.name)
    matches.sort()
    return _find_item(
        "enron-find-layforwarded2001",
        "List emails forwarded by Lay during 2001.",
        all_matches=matches,
    )


def _recipe_earliest_california(ingest: Path) -> dict:
    """The date of the earliest California-mentioning email (sentinel-filtered)."""
    dated: list[tuple[datetime, str]] = []
    for name in _grep(ingest, "california"):
        dt = _parse_email_date(ingest / name)
        if dt is not None:
            dated.append((dt, name))
    if not dated:
        raise RuntimeError("no California-mentioning emails found after the sentinel filter")
    dated.sort()
    earliest_dt, earliest_name = dated[0]
    return {
        "id": "enron-lookup-earliest-california",
        "question": "What was the date of the earliest email about California in the corpus?",
        "clause_category": "n/a",
        "gold_doc": earliest_name,
        "answer_key": earliest_dt.date().isoformat(),
        "type": "lookup",
    }


def _recipe_skilling_last(ingest: Path) -> dict:
    """The subject of the last email Skilling sent before his 2001-08-14 resignation."""
    candidates: list[tuple[datetime, str, str]] = []
    for p in sorted(ingest.glob("*.eml")):
        try:
            msg = email.message_from_string(p.read_text(errors="replace"))
        except Exception:
            continue
        sender = (msg.get("From") or "").lower()
        if not any(a in sender for a in SKILLING_ADDRS):
            continue
        dt = _parse_email_date(p)
        if dt is None:
            continue
        dt_utc = dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
        if dt_utc >= SKILLING_RESIGN_DATE:
            continue
        candidates.append((dt_utc, p.name, msg.get("Subject") or ""))
    if not candidates:
        raise RuntimeError("no pre-resignation Skilling emails found")
    candidates.sort()
    _, latest_name, subject = candidates[-1]
    return {
        "id": "enron-lookup-skilling-last-pre-resign",
        "question": "What was the subject of the last email Skilling sent before his resignation?",
        "clause_category": "n/a",
        "gold_doc": latest_name,
        "answer_key": subject.strip(),
        "type": "lookup",
    }


RECIPES = [
    _recipe_raptor,
    _recipe_ljm,
    _recipe_offbalance,
    _recipe_andersen,
    _recipe_ferc,
    _recipe_lay_forwarded_2001,
    _recipe_earliest_california,
    _recipe_skilling_last,
]


def _recipe_negative(slug: str, term: str) -> dict:
    return {
        "id": f"enron-find-neg-{slug}",
        "question": f"Find emails about {term}.",
        "clause_category": "n/a",
        "gold_doc": "(none expected)",
        "answer_key": {"count": 0, "all": [], "sample": []},
        "type": "find",
    }


# ── orchestrator ────────────────────────────────────────────────────────────


def generate(ingest_dir: Path, out_path: Path) -> int:
    """Emit the Enron ground-truth YAML. Returns the number of items written.

    Refuses to write (raises RuntimeError) if a negative term unexpectedly
    matches in the corpus — that signals a corpus shift and the negative needs
    to be re-chosen.
    """
    # Validate negatives first — if a term unexpectedly matches, refuse to proceed
    # (signals corpus drift; the negative needs to be re-chosen before recipes run).
    for _slug, term in NEGATIVE_TERMS:
        hits = _grep(ingest_dir, term)
        if hits:
            raise RuntimeError(
                f"negative term {term!r} unexpectedly has {len(hits)} hit(s) "
                f"(first: {hits[0]}); pick a different absent term"
            )
    items: list[dict] = [recipe(ingest_dir) for recipe in RECIPES]
    for slug, term in NEGATIVE_TERMS:
        items.append(_recipe_negative(slug, term))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump({"corpus": "enron", "items": items}, sort_keys=False))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the Enron answer-eval ground-truth set.")
    ap.add_argument("--ingest-dir", type=Path, required=True, help="Enron ingest dir (*.eml)")
    ap.add_argument("--out", type=Path, required=True, help="output enron.yaml")
    a = ap.parse_args(argv)
    n = generate(ingest_dir=a.ingest_dir, out_path=a.out)
    print(f"wrote {n} ground-truth items -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
