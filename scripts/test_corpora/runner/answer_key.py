"""Score an answer against an answer key, without a judge or a baseline (#661's bake-off, 2026-09-21).

A judge grades against a reference it cannot check. Where the corpus carries human labels (CUAD's
`master_clauses.csv`: 41 clause types per contract, annotated by lawyers) an answer can be scored against those
instead, for nothing, and recall becomes visible: a baseline that names 23 of 65 anti-assignment contracts looks
complete to every judge.

The labels are the annotators'. Everything else here is this harness's and is crude on purpose, so that it can be
read: a fact is present or it is not; a list is the F1 of the contracts an answer names against the contracts
labelled. What counts as "naming" a contract is the weakest part, and `named_contracts` says exactly what it does.

Key entries (see `corpora/cuad_key.py`, which writes them):
    {"kind": "fact",  "all_of": [["california"], ["exxonmobil", "exxon mobil"]]}    every group, any spelling in it
    {"kind": "date",  "date": "2022-03-15"}
    {"kind": "list",  "positives": ["<contract title>", ...], "universe": ["<every ingested title>", ...]}
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october")
_MONTHS += ("november", "december")


def normalize(text: str) -> str:
    """Lower-case letters and digits only. Typographic hyphens, underscores and spacing all vanish, which is what
    lets `EX‑10.4_Co‑Branding` in an answer match `EX-10.4_Co-Branding` in a filename."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def date_spellings(iso: str) -> set[str]:
    """Normalised ways an answer may write `iso`: March 15, 2022; 15 March 2022; Mar 15 2022; 2022-03-15;
    3/15/2022; 03/15/22. Ordinals ("15th") are handled by the caller stripping them. Day-first numeric forms are
    left out: 3/4/2022 is two different days and the corpus is American."""
    d = date.fromisoformat(iso)
    month, short = _MONTHS[d.month - 1], _MONTHS[d.month - 1][:3]
    forms = {f"{d.year}{d.month:02d}{d.day:02d}"}
    for name in (month, short):
        for day in {f"{d.day}", f"{d.day:02d}"}:  # contracts write "Nov. 02, 2019" as readily as "Nov. 2, 2019"
            forms |= {f"{name}{day}{d.year}", f"{day}{name}{d.year}"}
    for m in {f"{d.month}", f"{d.month:02d}"}:
        for day in {f"{d.day}", f"{d.day:02d}"}:
            forms |= {f"{m}/{day}/{d.year}", f"{m}/{day}/{d.year % 100:02d}"}
    return forms


def _has_date(text: str, iso: str) -> bool:
    low = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", text.lower())
    low = re.sub(r"(?<=\d)\s+(day\s+)?of\s+(?=[a-z])", " ", low)  # "the 15th day of March, 2022"
    squeezed = normalize(low)
    for form in date_spellings(iso):
        if "/" in form:
            if re.search(rf"(?<![\d/]){re.escape(form)}(?![\d/])", low):
                return True
        elif form in squeezed:
            return True
    return False


def company(title: str) -> str:
    """The filer's name as CUAD's filenames carry it: everything before the first underscore."""
    return normalize(title.split("_")[0])


def named_contracts(text: str, cited_titles: list[str], universe: list[str]) -> set[str]:
    """Which contracts of `universe` an answer names. A contract is named if the answer cites it, if its whole
    title appears in the text, or if its filer's name appears and that filer has exactly one contract in the
    universe and a name of eight characters or more. It misses a contract referred to only by paraphrase ("the
    Papa John's deal") and cannot tell a contract listed as a match from one listed as a non-match."""
    squeezed = normalize(text)
    cited = {normalize(t) for t in cited_titles if t}
    filers: dict[str, list[str]] = {}
    for title in universe:
        filers.setdefault(company(title), []).append(title)
    found = set()
    for title in universe:
        whole, filer = normalize(title), company(title)
        if whole in cited or whole in squeezed or len(filer) >= 8 and len(filers[filer]) == 1 and filer in squeezed:
            found.add(title)
    return found


def score(entry: dict, answer: str, cited_titles: list[str] | None = None) -> dict:
    """{"score": 0..1, ...detail}. An empty answer scores 0."""
    answer = answer or ""
    kind = entry["kind"]
    if kind == "fact":
        squeezed = normalize(answer)
        hits = [any(normalize(alt) in squeezed for alt in group) for group in entry["all_of"]]
        return {"score": sum(hits) / len(hits), "groups_found": sum(hits), "groups": len(hits)}
    if kind == "date":
        return {"score": 1.0 if _has_date(answer, entry["date"]) else 0.0}
    if kind == "list":
        want = set(entry["positives"])
        got = named_contracts(answer, cited_titles or [], entry["universe"])
        right = len(want & got)
        precision = right / len(got) if got else 0.0
        recall = right / len(want) if want else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return {
            "score": f1,
            "named": len(got),
            "right": right,
            "labelled": len(want),
            "precision": round(precision, 3),
            "recall": round(recall, 3),
        }
    raise ValueError(f"unknown key entry kind {kind!r}")
