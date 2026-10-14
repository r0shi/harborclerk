"""Questions with an answer key for the synthetic corpus, written from its own fact sidecars.

    uv --project scripts/test_corpora run python -m scripts.test_corpora.corpora.synthetic_key \\
        --workdir "$WORKDIR" --out-dir scripts/test_corpora/questions/keyed \\
        --snapshot scripts/test_corpora/tests/fixtures/synthetic_corpus [--redo]

reads `<workdir>/synthetic/facts/*.json` (one sidecar per generated document: vendor, dates, totals, signatories;
`corpora/synthetic.py` writes them beside the watched folder, never in it, #726) and the names of the files in
`<workdir>/synthetic/ingest`, and writes `questions/keyed/synthetic.yaml` (the sweep's format, `ask` only) and
`questions/keyed/synthetic.key.json` (what `runner/answer_key.py` scores against). Both are committed, like CUAD's.

The key is tied to the corpus it was counted over. Every entry carries the generation id (`answer_key.generation_id`,
a digest of the sidecars) and the file's `_generation` header records it with the document count; the scorer
refuses to score an entry against any other generation. `--snapshot` copies what the build read into a fixture
directory (every sidecar, the ingest file names, and the texts of the documents it checked a key against), so the
tests rebuild the committed key from the fixture and fail when a commit goes stale.

What is asked is chosen by rules, not by hand, so that the same sidecars give the same key and a regenerated corpus
gives a key in the same shape:

- A single document is asked about only when its name in the question is unambiguous (an invoice number no other
  invoice carries; the one 24-month contract with a vendor; a policy whose name and version no other policy shares;
  an onboarding letter addressed to a person rather than a template), only when it was kept as text (a document
  rendered to PDF for the OCR subset cannot be checked), and only when its key is in its own words: a sidecar whose
  sender the memo does not name is the generator's slip, and a model answering from the document would be marked
  wrong for it. One question per document.
- Counts, totals and lists are aggregates over every usable sidecar of a type, with the subject picked by a stated
  tie-break (the vendor with the fewest invoices, the quarter with the most board meetings) so that each sidecar
  field is asked about once. They are not checked against text: the sidecar's number is the generator's, written
  with the document; `--verify-aggregates` reports any contributor whose own document does not state its number.
- A sidecar that records a generation failure (`_error`) is unusable and counts for nothing but the header; the
  document it describes is still in the corpus, so the generation id covers it.
- The marketing briefs' budgets are not totalled: on the 2026-09-26 generation four of the seven Skylight briefs
  state no budget, or another one, in their text, so the sidecars' sum is an answer no document supports.
- A list names its items by file stem (what the app shows as the title) and by any alias unique in the corpus: the
  invoice number, the total to the cent, the total's whole dollars.
- The bilingual pairs ask one key in English and French (`cross_language`), and the key has the same entry under
  both variant ids, because that is how the sweep looks them up.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from scripts.test_corpora.runner.answer_key import generation_id, mentions, normalize, score

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent.parent
OUT_DIR = HERE / "questions" / "keyed"
SNAPSHOT_DIR = HERE / "tests" / "fixtures" / "synthetic_corpus"
COMPANY = "Marbledock & Associates"
ID_PREFIX = "synthetic-key"
# Words a generated onboarding letter uses in place of a name ("New Team Member", "Nouvelle recrue / New Hire").
_PLACEHOLDER_WORDS = frozenset(
    {
        *("new", "nouvelle", "nouvel", "nouveau", "employee", "employe", "recrue", "hire", "hired", "member", "membre"),
        *("colleague", "collegue", "team", "analyst", "analyste", "coordinator", "specified", "name", "nom"),
        *("template", "recipient", "candidate", "generic", "not", "unnamed"),
    }
)
_HONORIFIC = re.compile(r"^(?:m|mme|mlle|mr|mrs|ms|dr)\.?\s+", re.I)


class SnapshotIncomplete(RuntimeError):
    """A corpus directory lists a text document the build must read, and the file is not there."""


@dataclass
class Corpus:
    """What the build reads: `<root>/facts/*.json`, the names in `<root>/ingest` (or `<root>/ingest_names.txt`, in a
    snapshot) and, on demand, `<root>/ingest/<stem>.txt`. `texts_read` is what `--snapshot` copies."""

    root: Path
    sidecars: dict[str, dict]
    unusable: list[str]
    ingest_names: list[str]
    texts_read: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, root: Path) -> Corpus:
        root = Path(root)
        sidecars, unusable = {}, []
        for path in sorted((root / "facts").glob("*.json")):
            facts = json.loads(path.read_text())
            if not isinstance(facts, dict) or "_error" in facts or "_facts" in facts:
                unusable.append(path.stem)
            else:
                sidecars[path.stem] = facts
        listing = root / "ingest_names.txt"
        if listing.exists():
            names = listing.read_text().split()
        else:
            names = [p.name for p in (root / "ingest").iterdir() if p.suffix in {".txt", ".pdf"}]
        return cls(root=root, sidecars=sidecars, unusable=unusable, ingest_names=sorted(names))

    @property
    def generation(self) -> str:
        return generation_id(self.root / "facts")

    def of_type(self, doc_type: str) -> dict[str, dict]:
        return {s: f for s, f in self.sidecars.items() if s.split("_", 1)[1] == doc_type}

    def is_text(self, stem: str) -> bool:
        return f"{stem}.txt" in self.ingest_names

    def text(self, stem: str) -> str:
        path = self.root / "ingest" / f"{stem}.txt"
        if not path.exists():
            raise SnapshotIncomplete(f"{path} is listed but not present; rebuild the snapshot (--snapshot --redo)")
        self.texts_read.add(stem)
        return path.read_text(errors="replace")


# ── reading sidecar values, which the generator did not always shape the same way ──


def _name(value) -> str | None:
    """A person or office from a sidecar field that is a string ("Helena Voss, Chief Operations Officer") or a dict
    with a name in it: the part before the first comma, without an honorific."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("signatory_name")
    if not isinstance(value, str) or not value.strip():
        return None
    return _HONORIFIC.sub("", value.split(",")[0].strip()) or None


def _number(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except ArithmeticError:
        return None


def _iso(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def _quarter(iso: str) -> int:
    return (int(iso[5:7]) - 1) // 3 + 1


def _money(value: Decimal) -> float | int:
    return int(value) if value == value.to_integral_value() else float(value)


def _plain(value: Decimal) -> str:
    return f"{value:,.2f}" if value != value.to_integral_value() else f"{int(value):,}"


def _person_name(value) -> str | None:
    """An employee name that is a name: two to four capitalised words, none of them a template's."""
    if not isinstance(value, str) or re.search(r"[()\[\]/—–]", value):
        return None
    words = value.split()
    if not 2 <= len(words) <= 4 or not all(w[:1].isupper() for w in words):
        return None
    if any(normalize(w) in _PLACEHOLDER_WORDS for w in words):
        return None
    return value.strip()


def _version_key(value) -> str | None:
    """`v2.1.0`, `2.1` and `2.1.0` are one version."""
    if value is None:
        return None
    parts = str(value).strip().lstrip("vV").split(".")
    if not all(p.isdigit() for p in parts):
        return None
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def _name_key(value) -> str:
    """A policy name as a set of words, so that "A, B & C Policy" and "A, B, and C Policy" are one name."""
    if not isinstance(value, str):
        return ""
    return " ".join(sorted({normalize(w) for w in value.replace("&", " and ").split()} - {""}))


def _owner_alternatives(owner: str) -> list[str]:
    """The owner as written, with "&" as "and", without the company after the comma, and with "Human Resources"
    as "HR", in every combination that differs."""
    forms = []
    for base in (owner, owner.split(f", {COMPANY}")[0]):
        for alt in (base, base.replace("&", "and")):
            for form in (alt, re.sub(r"\bHuman Resources\b", "HR", alt)):
                if form.strip() and form.strip() not in forms:
                    forms.append(form.strip())
    return forms


def _name_alternatives(name: str) -> list[str]:
    """A person as written, without a middle initial ("Victoria L. Hargrove" is "Victoria Hargrove"), and by a
    surname of six letters or more ("Mme Côté-Renaud") when every word is capitalised as a person's are. An office
    ("Office of the Managing Partner") is itself, and "Partner" is nobody's surname."""
    forms = [name]
    without_initial = re.sub(r"\s+[A-Z]\.?(?=\s)", "", name)
    if without_initial != name:
        forms.append(without_initial)
    words = name.split()
    surname = words[-1]
    if 2 <= len(words) <= 4 and all(w[:1].isupper() for w in words) and len(normalize(surname)) >= 6:
        forms.append(surname)
    return forms


_SPELLINGS = (("harbour", "harbor"), ("colour", "color"), ("centre", "center"))


def _respell(name: str, this: str, that: str) -> str:
    """`this` as `that` wherever it occurs in `name`, keeping the capital: "Harbour Lights" -> "Harbor Lights"."""
    return re.sub(this, lambda m: that.capitalize() if m.group(0)[:1].isupper() else that, name, flags=re.I)


def _spelling_aliases(names: list[str]) -> dict[str, list[str]]:
    """The other side of the Atlantic for a name: "Harbour Lights" is also "Harbor Lights"."""
    aliases = {}
    for name in names:
        other = name
        for british, american in _SPELLINGS:
            other = (
                _respell(_respell(other, british, american), american, british)
                if american.lower() in name.lower()
                else _respell(other, british, american)
            )
        if other != name:
            aliases[name] = [other]
    return aliases


def _vendor_alternatives(vendor: str, vendors: list[str]) -> list[str]:
    """The vendor's full name, and its first word when no other vendor's starts the same and it is a word of five
    letters or more ("Globex" for "Globex Supplies")."""
    first = vendor.split()[0]
    others = [v.split()[0].lower() for v in vendors if v != vendor]
    return [vendor] + ([first] if len(first) >= 5 and first.lower() not in others else [])


# ── the build ──


class _Builder:
    def __init__(self, corpus: Corpus):
        self.corpus = corpus
        self.generation = corpus.generation
        self.questions: list[dict] = []
        self.key: dict[str, dict] = {}
        self.rejected: list[str] = []
        self.asked: set[str] = set()

    def add(self, text: str, entry: dict, notes: str, *, fr: str | None = None, document: str | None = None) -> bool:
        """One question. With `document`, the entry must score 1.0 against that document's text, else it is not
        asked (and `rejected` says why); the document is then asked about once. With `fr`, a bilingual pair."""
        if document is not None:
            if document in self.asked:
                return False
            if not self.corpus.is_text(document):
                self.rejected.append(f"{document}: rendered to PDF, cannot be checked against its text")
                return False
            if score(entry, self.corpus.text(document))["score"] < 1.0:
                self.rejected.append(
                    f"{document}: the key {entry.get('all_of') or entry.get('date') or entry.get('amount')!r} is not in its text"
                )
                return False
            self.asked.add(document)
            entry = {**entry, "document": document}
        qid = f"{ID_PREFIX}-{len(self.questions) + 1}"
        entry = {**entry, "generation": self.generation}
        if fr is None:
            self.questions.append({"id": qid, "text": text, "notes": notes})
            self.key[qid] = entry
        else:
            variants = [{"lang": "en", "text": text}, {"lang": "fr", "text": fr}]
            self.questions.append({"id": qid, "cross_language": True, "variants": variants, "notes": notes})
            for lang in ("en", "fr"):  # the sweep asks `<id>__<lang>` and looks the key up by that id
                self.key[f"{qid}__{lang}"] = dict(entry)
        return True

    # Each method is one recipe. The order here is the order of the questions.

    def invoices(self) -> None:
        c = self.corpus
        invoices = {
            s: f
            for s, f in c.of_type("invoice").items()
            if isinstance(f.get("vendor"), str) and _iso(f.get("date")) and _number(f.get("total_usd")) is not None
        }
        vendors = sorted({f["vendor"] for f in invoices.values()})
        numbers = Counter(normalize(str(f.get("invoice_number"))) for f in invoices.values())
        # One representative invoice per vendor (the first, by stem, with an invoice number no other invoice has),
        # each asked one kind of thing, so that no two lookups have the same shape.
        roles = ("amount", "fact", "date")
        for i, vendor in enumerate(vendors):
            for stem, f in sorted(invoices.items()):
                if (
                    f["vendor"] != vendor
                    or numbers[normalize(str(f.get("invoice_number")))] != 1
                    or not c.is_text(stem)
                ):
                    continue
                number, total = f["invoice_number"], _number(f["total_usd"])
                role = roles[i % len(roles)]
                if role == "amount":
                    asked = self.add(
                        f"What is the total amount due on invoice {number} from {vendor} to {COMPANY}?",
                        {"kind": "amount", "amount": _money(total)},
                        f"total_usd = {_plain(total)} in facts/{stem}.json; invoice_number is unique in the corpus",
                        document=stem,
                    )
                elif role == "fact":
                    asked = self.add(
                        f"Which vendor issued invoice {number} to {COMPANY}?",
                        {"kind": "fact", "all_of": [_vendor_alternatives(vendor, vendors)]},
                        f"vendor = {vendor!r} in facts/{stem}.json; invoice_number is unique in the corpus",
                        document=stem,
                    )
                else:
                    asked = self.add(
                        f"On what date was invoice {number} from {vendor} issued?",
                        {"kind": "date", "date": f["date"], "day_first": True, "year_optional": True},
                        f"date = {f['date']} in facts/{stem}.json; invoice_number is unique in the corpus",
                        document=stem,
                    )
                if asked:
                    break
        if not vendors:
            return
        by_vendor = defaultdict(list)
        for stem, f in sorted(invoices.items()):
            by_vendor[f["vendor"]].append(stem)
        fewest = min(vendors, key=lambda v: (len(by_vendor[v]), v))
        most = max(vendors, key=lambda v: (len(by_vendor[v]), v))
        years = {f["date"][:4] for f in invoices.values()}
        year = min(years) if len(years) == 1 else None
        when_en = f" in {year}" if year else ""
        when_fr = f" en {year}" if year else ""
        self.add(
            f"How many invoices did {fewest} issue to {COMPANY}{when_en}?",
            {"kind": "count", "count": len(by_vendor[fewest]), "field": "vendor", "documents": by_vendor[fewest]},
            f"{len(by_vendor[fewest])} invoice sidecars with vendor = {fewest!r} (the vendor with the fewest)",
            fr=f"Combien de factures {fewest} a-t-elle émises à {COMPANY}{when_fr} ?",
        )
        total = sum(_number(invoices[s]["total_usd"]) for s in by_vendor[most])
        self.add(
            f"What is the total amount invoiced by {most} to {COMPANY}{when_en}, across all its invoices?",
            {"kind": "amount", "amount": _money(total), "field": "total_usd", "documents": by_vendor[most]},
            f"sum of total_usd over the {len(by_vendor[most])} invoice sidecars with vendor = {most!r} (the vendor "
            f"with the most) = {_plain(total)}",
        )
        by_month = defaultdict(list)
        for stem, f in sorted(invoices.items()):
            by_month[f["date"][:7]].append(stem)
        months = sorted(m for m in by_month if len(by_month[m]) >= 2)
        if months:
            month = min(months, key=lambda m: (len(by_month[m]), m))
            total = sum(_number(invoices[s]["total_usd"]) for s in by_month[month])
            self.add(
                f"What was the total amount invoiced to {COMPANY} in {_month_name(month)}, across all vendors?",
                {"kind": "amount", "amount": _money(total), "field": "total_usd", "documents": by_month[month]},
                f"sum of total_usd over the {len(by_month[month])} invoice sidecars dated {month}-* (the month with "
                f"the fewest invoices, two or more) = {_plain(total)}",
            )
        universe = sorted(invoices)
        aliases = _invoice_aliases(invoices, c)
        self.add(
            f"List every invoice {fewest} issued to {COMPANY}{when_en}, with its invoice number and total.",
            {
                "kind": "list",
                "positives": by_vendor[fewest],
                "universe": universe,
                "aliases": aliases,
                "field": "vendor",
                "documents": by_vendor[fewest],
            },
            f"the {len(by_vendor[fewest])} invoice sidecars with vendor = {fewest!r}, of {len(universe)} invoices; an "
            "invoice is named by its file stem, or by its invoice number or total where unique",
        )
        busiest = max(by_month, key=lambda m: (len(by_month[m]), -int(m.replace("-", ""))))
        self.add(
            f"List every invoice issued to {COMPANY} in {_month_name(busiest)}, from any vendor, with its invoice "
            "number and total.",
            {
                "kind": "list",
                "positives": by_month[busiest],
                "universe": universe,
                "aliases": aliases,
                "field": "date",
                "documents": by_month[busiest],
            },
            f"the {len(by_month[busiest])} invoice sidecars dated {busiest}-* (the month with the most invoices), of "
            f"{len(universe)} invoices",
        )

    def vendor_contracts(self) -> None:
        contracts = {
            s: f
            for s, f in self.corpus.of_type("vendor_contract").items()
            if isinstance(f.get("vendor"), str) and _number(f.get("term_months")) is not None
        }
        shapes = Counter((f["vendor"], int(_number(f["term_months"]))) for f in contracts.values())
        unique = [s for s, f in sorted(contracts.items()) if shapes[(f["vendor"], int(_number(f["term_months"])))] == 1]
        asked = 0
        for stem in unique:
            f = contracts[stem]
            term, vendor = int(_number(f["term_months"])), f["vendor"]
            about = f"the {term}-month vendor service contract between {COMPANY} and {vendor}"
            fee, law = _number(f.get("monthly_fee_usd")), f.get("governing_law")
            if asked % 2 == 0 and fee is not None:
                asked += self.add(
                    f"What is the monthly fee in {about}?",
                    {"kind": "amount", "amount": _money(fee)},
                    f"monthly_fee_usd = {_plain(fee)} in facts/{stem}.json; the only {term}-month contract with {vendor}",
                    document=stem,
                )
            elif isinstance(law, str) and law.strip():
                short = re.sub(r"^(the )?(state|province|laws?) of ", "", law.strip(), flags=re.I)
                asked += self.add(
                    f"What is the governing law of {about}?",
                    {"kind": "fact", "all_of": [[short]]},
                    f"governing_law = {law!r} in facts/{stem}.json; the only {term}-month contract with {vendor}",
                    document=stem,
                )
            if asked >= 2:
                break

    def onboarding(self) -> None:
        letters = self.corpus.of_type("onboarding_letter")
        named = {s: f for s, f in sorted(letters.items()) if _person_name(f.get("employee_name"))}
        names = Counter(normalize(f["employee_name"]) for f in named.values())
        roles = ("date", "fact")
        for i, (stem, f) in enumerate(s for s in named.items() if names[normalize(s[1]["employee_name"])] == 1):
            who = _person_name(f["employee_name"])
            if roles[i % 2] == "date" and _iso(f.get("start_date")):
                self.add(
                    f"On what date does {who} start at {COMPANY}, according to the onboarding letter?",
                    {"kind": "date", "date": f["start_date"], "day_first": True, "year_optional": True},
                    f"start_date = {f['start_date']} in facts/{stem}.json; the one letter addressed to a named person"
                    " of that name",
                    fr=f"À quelle date {who} entre-t-elle en fonction chez {COMPANY}, selon la lettre d'accueil ?",
                    document=stem,
                )
            elif (signer := _name(f.get("signing_manager"))) is not None:
                self.add(
                    f"Who signed the onboarding letter addressed to {who}?",
                    {"kind": "fact", "all_of": [_name_alternatives(signer)]},
                    f"signing_manager = {signer!r} in facts/{stem}.json; the one letter addressed to {who}",
                    document=stem,
                )

    def policies(self) -> None:
        policies = {
            s: f
            for s, f in self.corpus.of_type("policy_doc").items()
            if isinstance(f.get("policy_name"), str) and _version_key(f.get("version"))
        }
        shapes = Counter((_name_key(f["policy_name"]), _version_key(f["version"])) for f in policies.values())
        roles = ("fact", "date")
        asked = 0
        for stem, f in sorted(policies.items()):
            if shapes[(_name_key(f["policy_name"]), _version_key(f["version"]))] != 1:
                continue
            name, version = f["policy_name"].strip(), str(f["version"]).strip()
            about = f"version {version} of the {name}"
            if roles[asked % 2] == "fact" and isinstance(f.get("owner"), str) and f["owner"].strip():
                asked += self.add(
                    f"Who is the policy owner of {about}?",
                    {"kind": "fact", "all_of": [_owner_alternatives(f["owner"].strip())]},
                    f"owner = {f['owner'].strip()!r} in facts/{stem}.json; no other policy has that name and version",
                    document=stem,
                )
            elif _iso(f.get("effective_date")):
                asked += self.add(
                    f"On what date did {about} take effect?",
                    {"kind": "date", "date": f["effective_date"], "day_first": True, "year_optional": True},
                    f"effective_date = {f['effective_date']} in facts/{stem}.json; no other policy has that name and version",
                    document=stem,
                )
            if asked >= 2:
                break

    def memos(self) -> None:
        memos = {s: f for s, f in self.corpus.of_type("internal_memo").items() if isinstance(f.get("subject"), str)}
        subjects = Counter(normalize(f["subject"]) for f in memos.values())
        for stem, f in sorted(memos.items()):
            sender = _name(f.get("from"))
            if subjects[normalize(f["subject"])] != 1 or sender is None:
                continue
            if self.add(
                f"Who signed the internal memo with the subject “{f['subject'].strip()}”?",
                {"kind": "fact", "all_of": [_name_alternatives(sender)]},
                f"from = {f['from']!r} in facts/{stem}.json, read as {sender!r}; the one memo with that subject. The "
                "signature block names the signer; the FROM header may name an office, which is not credited",
                document=stem,
            ):
                break

    def board(self) -> None:
        minutes = {s: f for s, f in self.corpus.of_type("board_minutes").items() if _iso(f.get("date"))}
        by_month, by_quarter = defaultdict(list), defaultdict(list)
        for stem, f in sorted(minutes.items()):
            by_month[f["date"][:7]].append(stem)
            by_quarter[(f["date"][:4], _quarter(f["date"]))].append(stem)
        asked = 0
        for month in sorted(by_month):
            if len(by_month[month]) != 1 or asked >= 2:
                continue
            (stem,) = by_month[month]
            asked += self.add(
                f"On what date did the board of {COMPANY} meet in {_month_name(month)}?",
                {"kind": "date", "date": minutes[stem]["date"], "day_first": True, "year_optional": True},
                f"date = {minutes[stem]['date']} in facts/{stem}.json; the only board minutes dated {month}",
                document=stem,
            )
        if by_quarter:
            year, q = max(by_quarter, key=lambda k: (len(by_quarter[k]), -k[1], k[0]))
            stems = by_quarter[(year, q)]
            self.add(
                f"How many board meetings did {COMPANY} hold in Q{q} {year}, according to the board minutes?",
                {"kind": "count", "count": len(stems), "field": "date", "documents": stems},
                f"{len(stems)} board_minutes sidecars dated in Q{q} {year} (the quarter with the most meetings)",
            )

    def marketing(self) -> None:
        briefs = {
            s: f
            for s, f in self.corpus.of_type("marketing_brief").items()
            if isinstance(f.get("campaign_name"), str) and isinstance(f.get("target"), str)
        }
        by_campaign, by_client = defaultdict(list), defaultdict(set)
        for stem, f in sorted(briefs.items()):
            by_campaign[f["campaign_name"].strip()].append(stem)
            by_client[f["target"].strip()].add(f["campaign_name"].strip())
        campaigns = sorted(by_campaign, key=lambda c: (-len(by_campaign[c]), c))
        clients = sorted(by_client)
        if len(campaigns) >= 1:
            name = campaigns[0]
            self.add(
                f"How many marketing briefs did {COMPANY} write for the {name} campaign?",
                {
                    "kind": "count",
                    "count": len(by_campaign[name]),
                    "field": "campaign_name",
                    "documents": by_campaign[name],
                },
                f"{len(by_campaign[name])} marketing_brief sidecars with campaign_name = {name!r} (the campaign with "
                "the most briefs; ties by name)",
            )
        if len(campaigns) >= 2:
            name = campaigns[1]
            targets = sorted({briefs[s]["target"].strip() for s in by_campaign[name]})
            self.add(
                f"Which clients do the marketing briefs for the {name} campaign target?",
                {
                    "kind": "list",
                    "positives": targets,
                    "universe": clients,
                    "field": "target",
                    "documents": by_campaign[name],
                },
                f"target of the {len(by_campaign[name])} marketing_brief sidecars with campaign_name = {name!r}: "
                f"{', '.join(targets)}; the universe is every client a brief targets",
                fr=f"Quels clients les briefs marketing de la campagne {name} ciblent-ils ?",
            )
        if clients:
            client = min(clients, key=lambda c: (len(by_client[c]), c))
            self.add(
                f"Which campaigns have a marketing brief that targets {client}?",
                {
                    "kind": "list",
                    "positives": sorted(by_client[client]),
                    "universe": sorted(by_campaign),
                    "aliases": _spelling_aliases(sorted(by_campaign)),
                    "field": "campaign_name",
                    "documents": [st for st in sorted(briefs) if briefs[st]["target"].strip() == client],
                },
                f"campaign_name of the marketing_brief sidecars with target = {client!r} (the client the fewest "
                f"campaigns target): {', '.join(sorted(by_client[client]))}",
            )

    def build(self) -> tuple[dict, dict]:
        self.invoices()
        self.vendor_contracts()
        self.onboarding()
        self.policies()
        self.memos()
        self.board()
        self.marketing()
        header = {
            "corpus": "synthetic",
            "generation": self.generation,
            "documents": len(self.corpus.ingest_names),
            "sidecars": len(self.corpus.sidecars) + len(self.corpus.unusable),
            "unusable_sidecars": len(self.corpus.unusable),
            "questions": len(self.questions),
            "built_by": "scripts.test_corpora.corpora.synthetic_key",
        }
        return {"research": [], "ask": self.questions}, {"_generation": header, **self.key}


def _invoice_aliases(invoices: dict[str, dict], corpus: Corpus) -> dict[str, list[str]]:
    """How an answer may name an invoice other than by its file stem: its invoice number, its total to the cent,
    its total in whole dollars, each only when no other invoice shares it and no other invoice's text states it
    (a subtotal that happens to equal another invoice's total would name the wrong invoice; review of #812). An
    invoice rendered to PDF cannot be read and is not consulted."""
    texts = {s: corpus.text(s) for s in invoices if corpus.is_text(s)}
    numbers = Counter(normalize(str(f.get("invoice_number"))) for f in invoices.values())
    cents = Counter(f"{_number(f['total_usd']):.2f}" for f in invoices.values())
    dollars = Counter(str(int(_number(f["total_usd"]))) for f in invoices.values())
    aliases = {}
    for stem, f in invoices.items():
        total = _number(f["total_usd"])
        candidates = []
        if f.get("invoice_number") and numbers[normalize(str(f["invoice_number"]))] == 1:
            candidates.append(str(f["invoice_number"]))
        if cents[f"{total:.2f}"] == 1:
            candidates.append(f"{total:.2f}")
        if dollars[str(int(total))] == 1:
            candidates.append(str(int(total)))
        aliases[stem] = [a for a in candidates if not any(mentions(t, a) for s, t in texts.items() if s != stem)]
    return aliases


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October")
_MONTH_NAMES += ("November", "December")


def _month_name(year_month: str) -> str:
    return f"{_MONTH_NAMES[int(year_month[5:7]) - 1]} {year_month[:4]}"


def build(corpus: Corpus) -> tuple[dict, dict]:
    """(questions, key) for `corpus`; `corpus.texts_read` then names the documents a key was checked against."""
    builder = _Builder(corpus)
    questions, key = builder.build()
    for reason in builder.rejected:
        log.info("not asked: %s", reason)
    return questions, key


def verify_aggregates(corpus: Corpus, key: dict) -> list[str]:
    """For every count, amount or list entry, the contributors whose own text does not state the sidecar value the
    aggregate used (`entry["field"]`): `"<qid> <stem>: total_usd 20080.38 not in its text"`. A PDF contributor
    cannot be read and is listed as such. Reporting only; the key is the sidecars'."""
    problems = []
    for qid, entry in key.items():
        if qid == "_generation" or entry["kind"] not in ("count", "amount", "list") or "field" not in entry:
            continue
        field_name = entry["field"]
        for stem in entry.get("documents") or []:
            facts = corpus.sidecars.get(stem)
            if facts is None or field_name not in facts:
                continue
            if not corpus.is_text(stem):
                problems.append(f"{qid} {stem}: rendered to PDF, not checked")
                continue
            value, text = facts[field_name], corpus.text(stem)
            if (number := _number(value)) is not None and field_name.endswith("_usd"):
                check = {"kind": "amount", "amount": _money(number)}
            elif _iso(value):
                check = {"kind": "date", "date": value, "day_first": True}
            else:
                check = {"kind": "fact", "all_of": [[str(value)]]}
            if score(check, text)["score"] < 1.0:
                problems.append(f"{qid} {stem}: {field_name} {value!r} not in its text")
    return problems


def write_snapshot(corpus: Corpus, out: Path) -> None:
    """Copy what the build read into `out`: every sidecar, the ingest file names, the texts it checked keys against."""
    if out.exists():
        shutil.rmtree(out)
    (out / "facts").mkdir(parents=True)
    (out / "ingest").mkdir()
    for sidecar in sorted((corpus.root / "facts").glob("*.json")):
        shutil.copyfile(sidecar, out / "facts" / sidecar.name)
    (out / "ingest_names.txt").write_text("\n".join(corpus.ingest_names) + "\n")
    for stem in sorted(corpus.texts_read):
        shutil.copyfile(corpus.root / "ingest" / f"{stem}.txt", out / "ingest" / f"{stem}.txt")


def write(questions: dict, key: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "synthetic.yaml").write_text(yaml.safe_dump(questions, sort_keys=False, allow_unicode=True, width=110))
    (out_dir / "synthetic.key.json").write_text(json.dumps(key, indent=1, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--workdir", type=Path, help="the sweep's workdir; the corpus is <workdir>/synthetic")
    where.add_argument("--corpus-dir", type=Path, help="a corpus or snapshot directory holding facts/ and ingest/")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    p.add_argument("--snapshot", type=Path, nargs="?", const=SNAPSHOT_DIR, help="also write the test fixture here")
    p.add_argument("--redo", action="store_true", help="overwrite the questions, key and snapshot that are there")
    p.add_argument("--verify-aggregates", action="store_true", help="report contributors whose text lacks their value")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = args.corpus_dir if args.corpus_dir else args.workdir.expanduser() / "synthetic"
    targets = [args.out_dir / "synthetic.yaml", args.out_dir / "synthetic.key.json"]
    if args.snapshot:
        targets.append(args.snapshot)
    existing = [str(t) for t in targets if t.exists()]
    if existing and not args.redo:
        print("refusing to overwrite without --redo: " + ", ".join(existing), file=sys.stderr)
        return 2
    corpus = Corpus.load(root)
    questions, key = build(corpus)
    read_for_build = set(corpus.texts_read)  # the snapshot holds what the build read, not what verification read
    if args.verify_aggregates:
        for problem in verify_aggregates(corpus, key):
            print(f"aggregate contributor: {problem}")
    write(questions, key, args.out_dir)
    if args.snapshot:
        corpus.texts_read = read_for_build
        write_snapshot(corpus, args.snapshot)
    header = key["_generation"]
    print(
        f"{header['questions']} questions ({len(key) - 1} key entries) for synthetic generation "
        f"{header['generation']} ({header['documents']} documents, {header['unusable_sidecars']} unusable sidecars) "
        f"-> {args.out_dir}" + (f"; snapshot -> {args.snapshot}" if args.snapshot else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
