"""Score an answer against an answer key, without a judge or a baseline (#661's bake-off, 2026-09-21).

A judge grades against a reference it cannot check. Where the corpus carries labels (CUAD's `master_clauses.csv`:
41 clause types per contract, annotated by lawyers; the synthetic corpus's fact sidecars, written with each
document) an answer can be scored against those instead, for nothing, and recall becomes visible: a baseline that
names 23 of 65 anti-assignment contracts looks complete to every judge.

The labels are the annotators' or the generator's. Everything else here is this harness's and is crude on purpose,
so that it can be read: a fact is present or it is not; a list is the F1 of the items an answer names in its text
against the items labelled. What counts as "naming" an item is the weakest part, and `named_contracts` says exactly
what it does, and what it deliberately ignores. An answer may also name an item by its document id: a scoring site
passes `doc_ids`, the id -> title map built from the citations the answer's tools returned, and an id written in
the text that maps to a universe title names that item (a cloud model writes "SteelVault - Equidata Affiliate
Agreement (doc_id `ebbbd23d-...`)" and never the filename stem the key holds; bench-20261014-2328-cuad-fable scored
four such lists at 0 named). The citations themselves are still not counted: the map is a dictionary for the ids
the answer wrote out, nothing more. A list answer's sections are read before its items are: a cloud subject puts
what it rejected under its own heading ("## False positive (excluded)", "## Near-miss worth flagging (not
Delaware-governed)", "## Mentions / borderline (included for completeness, not operative ROFRs ...)") or bold
lead-in ("**One additional keyword match, not counted above:**"), and an item named there, by id, title or filer,
is not named (`without_excluded_sections`). A heading (`#` line) or a bold lead-in (`**...**` opening a line)
marks exclusion when its own text, accents folded, contains "excluded", "exclusion(s)", "excludes", "not counted",
"not included", "not a match", "non-match", "near-miss", "borderline", "false positive", "not operative" or "does
not": the phrasings observed in cloud subjects' answers, in English only (a French answer's "exclus" is not read; no
keyed list question is asked in French). A bare "no" is not one of them: on the real data it dropped only a
section holding a labelled contract ("— no affirmative covenant"), and it would drop "## Matches (no duplicates)".
A heading's section runs to the next heading of its level or higher, so its sub-headings and lead-ins go with it; a
lead-in's runs to the next lead-in or heading; the answer's first section is never dropped. The costs, accepted: a
genuinely matching item listed under a hedged heading is lost (on bench-20261014-2328-cuad-fable, a labelled
contract under "Mentions / borderline"); an item rejected in running prose ("X is not one") is still counted; and a
lead-in followed by `###` items ("**Not counted:**", then "### 1. Rand") excludes nothing but itself, because a
heading ends a lead-in's section. The fact matcher accepts negation ("New York law, not Delaware" finds
"Delaware"); a key is one phrase, so this has not mattered yet, and is stated so that a score of 1.0 is read as
"the phrase is present", nothing more. The one thing read before the phrase is looked for is the answer's opening
sentence: one that contains a "found nothing" phrasing ("is not explicitly stated in the retrieved excerpts", then
a party "in Nevada", the keyed answer; #713) scores 0, whatever the answer says after. That is the whole rule, and
its costs are accepted and pinned in the tests: a correct answer whose first sentence carries a caveat about
something else ("Although the venue is not explicitly stated, Nevada law governs") scores 0, and an answer that
states the parties first and the absence second scores 1.0. The phrasings are the harness's no-findings list in
`quality.py`, not a second list; some of them count only here, in an opening.

Key entries (see `corpora/cuad_key.py` and `corpora/synthetic_key.py`, which write them):
    {"kind": "fact",   "all_of": [["california"], ["exxonmobil", "exxon mobil"]]}   every group, any spelling in it
    {"kind": "date",   "date": "2022-03-15"}            + "day_first": true for 15/03/2022, "year_optional": true
                                                          for "March 15" when the question says which year
    {"kind": "count",  "count": 15}                     the first standalone integer the answer states, or its word
    {"kind": "amount", "amount": 364107.38}             a money amount, to the cent
    {"kind": "list",   "positives": ["<title>", ...], "universe": ["<every title>", ...],
                       "aliases": {"<title>": ["<other way the answer may name it>", ...]}}   aliases optional

A count is the first number the answer states: "Initech issued 14 invoices; Globex 20, and 15 for someone else"
scores 0 against 15, and "Of the 60 invoices, 15 are Initech's" scores 0 too, which is the rule's cost, pinned in
the tests. A key appearing anywhere was the alternative, and it scored a wrong answer right whenever the right
number came up for another reason (review of #812). Letters are compared with their accents folded (`normalize`):
"Remi Thibodeau" is "Rémi Thibodeau", because the synthetic corpus is bilingual and a model answering in English
drops them. Dates are read in English and French spellings, and in day-first numerals when the entry says so;
counts and amounts in either language's digit grouping. `counts_in` and `amounts_in` say what they strip and why.

A synthetic key entry also carries `"generation"`: the first sixteen hex digits of a SHA-256 over the corpus's fact
sidecars (`generation_id`). An entry with one is scored only against that corpus: `score` looks for the synthetic
corpus under the sweep's workdir, and refuses (`CorpusMismatch`) when it is missing or is another generation,
because a count of invoices is only true of the documents it was counted over. The sweep asks before it starts
(`refuse_foreign_keys`), so a foreign key stops the run before any unit, not at the first scored answer. The
workdir is the one bound with `bind_corpus_root` (the sweep binds its own), else `$HC_EVAL_WORKDIR`, else the
`--workdir` on this process's command line, else the harness default.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from scripts.test_corpora.runner.quality import nothing_found_signature

_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october")
_MONTHS += ("november", "december")
# French month names, accents folded as `normalize` folds them, with the usual abbreviations (mars, mai, juin and
# aout are not abbreviated).
_MONTHS_FR = ("janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre")
_MONTHS_FR += ("novembre", "decembre")
_MONTHS_FR_SHORT = ("janv", "fevr", "mars", "avr", "mai", "juin", "juil", "aout", "sept", "oct", "nov", "dec")
# What may stand in front of an answer's first sentence: a thinking model's reasoning block, a bold label on a
# line of its own (`**Answer:**`, `**Answer**:`), a list marker. Read past, so the sentence is the one judged. A
# label is bold text ending in a colon; bold text on its own line without one (`**Nevada.**`) is the answer, not a
# label, and a heading (`## Nevada`) is kept for the same reason (review of #764). The cost is the other way: the
# #713 answer under `## Governing Law`, or under a colon-less `**Answer**`, is not caught, and that is the miss
# the scorer had before this rule. Pinned in the tests as the decision it is.
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
_LABEL_LINE = re.compile(r"^(?:\*\*[^*\r\n]+:\*\*|\*\*[^*\r\n]+\*\*:)[ \t]*(?:\r?\n|$)")
_LIST_MARKER = re.compile(r"^(?:[-*•]|\d+[.)])\s+")
# A sentence ends at `.`, `!` or `?` (closing bold, quotes or brackets allowed after it) before whitespace and a
# capital, digit or opening mark, or the end, or at a line break. "Inc. and" is not an end; "U.S. Virgin" is.
_SENTENCE_END = re.compile(r"[.!?][*_)\]\"'”’]*(?=\s+[A-Z0-9\"“*(\[]|\s*$)|\r?\n")

GENERATION_DIGITS = 16
WORKDIR_ENV = "HC_EVAL_WORKDIR"


class CorpusMismatch(RuntimeError):
    """The key was written for another generation of the corpus than the one under the workdir, or there is none."""


def opening(answer: str) -> str:
    """The answer's first sentence, past a reasoning block, label lines and a list marker in front of it. An end
    read too early ("U.S." before "Virgin Islands") weakens the absence check, since less is read; it never
    touches the term match, which is over the whole answer."""
    text = _THINK.sub("", answer).lstrip()
    while found := _LABEL_LINE.match(text):
        text = text[found.end() :].lstrip()
    text = _LIST_MARKER.sub("", text, count=1)
    found = _SENTENCE_END.search(text)
    return text[: found.start()] if found else text


def fold(text: str) -> str:
    """`text` with its accents dropped (NFKD, combining marks removed): "Côté-Renaud" -> "Cote-Renaud"."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def normalize(text: str) -> str:
    """Lower-case letters and digits only, accents folded. Typographic hyphens, underscores and spacing all vanish,
    which is what lets `EX‑10.4_Co‑Branding` in an answer match `EX-10.4_Co-Branding` in a filename, and
    "Remi Thibodeau" match "Rémi Thibodeau"."""
    return re.sub(r"[^a-z0-9]", "", fold(text).lower())


def _words(text: str) -> str:
    """Letters and digits, lower-cased and folded, with one space where anything else was. Forms are matched
    between boundaries in this, so "12 November 2019" is not "2 November 2019" and a number inside a longer digit
    run is not that number (review of #697), while "February 4, 2020**【141b..." still is, because the "**【"
    keeps the 141 apart."""
    return re.sub(r"[^a-z0-9]+", " ", fold(text).lower())


# ── dates ──


def _month_names(d: date) -> tuple[str, ...]:
    return (_MONTHS[d.month - 1], _MONTHS[d.month - 1][:3], _MONTHS_FR[d.month - 1], _MONTHS_FR_SHORT[d.month - 1])


def date_spellings(iso: str, *, day_first: bool = False) -> set[str]:
    """Normalised ways an answer may write `iso`: March 15, 2022; 15 March 2022; Mar 15 2022; 15 mars 2022;
    2022-03-15; 3/15/2022; 03/15/22; 03-15-2022. Ordinals ("15th", "1er") are handled by the caller stripping them.
    Day-first numeric forms (15/03/2022, 15-03-2022, 15.03.2022) are included only with `day_first`: 3/4/2022 is
    two different days, and CUAD is American; the synthetic corpus is Canadian and bilingual, and its key asks for
    them."""
    d = date.fromisoformat(iso)
    forms = {f"{d.year}{d.month:02d}{d.day:02d}"}
    for name in _month_names(d):
        for day in {f"{d.day}", f"{d.day:02d}"}:  # contracts write "Nov. 02, 2019" as readily as "Nov. 2, 2019"
            forms |= {f"{name}{day}{d.year}", f"{day}{name}{d.year}"}
    for m in {f"{d.month}", f"{d.month:02d}"}:
        for day in {f"{d.day}", f"{d.day:02d}"}:
            for year in (f"{d.year}", f"{d.year % 100:02d}"):
                forms |= {f"{m}/{day}/{year}", f"{m}-{day}-{year}"}
                if day_first:
                    forms |= {f"{day}/{m}/{year}", f"{day}-{m}-{year}", f"{day}.{m}.{year}"}
    return forms


def month_day_spellings(iso: str) -> set[str]:
    """The word forms of `iso` without its year ("march15", "15mars"), for an entry with `year_optional`: the
    question named the year, so "The board met on January 12." is the answer. Numerals without a year are not
    read: 1/12 is two days."""
    d = date.fromisoformat(iso)
    forms = set()
    for name in _month_names(d):
        for day in {f"{d.day}", f"{d.day:02d}"}:
            forms |= {f"{name}{day}", f"{day}{name}"}
    return forms


def _has_date(text: str, iso: str, *, day_first: bool = False, year_optional: bool = False) -> bool:
    low = fold(text).lower()
    low = re.sub(r"(?<=\d)(st|nd|rd|th|er|re|e)\b", "", low)  # "15th", "1er décembre"
    low = re.sub(r"(?<=\d)\s+(day\s+)?of\s+(?=[a-z])", " ", low)  # "the 15th day of March, 2022"
    words = re.sub(r"[^a-z0-9]+", " ", low)
    for form in date_spellings(iso, day_first=day_first):
        if re.search(r"[/.-]", form):
            # A numeral form is read in the text itself, between non-digits: "13/15/2022" is not "3/15/2022", and
            # a trailing full stop ("on 15.03.2022.") is a sentence end, not another digit group.
            if re.search(rf"(?<!\d)(?<!\d[/.-])(?<!\d[/.-]\d)(?<!\d[/.-]\d\d){re.escape(form)}(?![/.-]?\d)", low):
                return True
        elif re.search(rf"(?<![a-z0-9])(?:{_spaced(form)})(?![a-z0-9])", words):
            return True
    if year_optional:
        for form in month_day_spellings(iso):  # not followed by a year, which would be another date's
            if re.search(rf"(?<![a-z0-9])(?:{_spaced(form)})(?![a-z0-9])(?!\s*(?:19|20)\d\d\b)", words):
                return True
    return False


def _spaced(form: str) -> str:
    """A squeezed form as a pattern over `_words` text: optional single spaces between its parts (`march152022`
    matches "march 15 2022", "march 15, 2022" and "march15 2022")."""
    return r" ?".join(re.escape(ch) for ch in form)


# ── lists ──


def company(title: str) -> str:
    """The filer's name as CUAD's filenames carry it: everything before the first underscore."""
    return normalize(title.split("_")[0])


# A markdown heading: one to six `#`, then its text. A bold lead-in: `**...**` opening a line. A numbered item written
# bold ("**1. Staar – Distributor** (doc_id ...)") opens its line bold and so is a lead-in of its own, which is
# benign: its section is itself, to the next lead-in or heading, and its own text is a name, not a marker. A
# bulleted one ("- **Staar**") is not a lead-in. What marks a section as the answer's rejects, read in its heading's
# or lead-in's own text only, lower-cased and accents folded, English only; the module docstring lists them in words.
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$")
_BOLD_LEAD = re.compile(r"^[ \t]*\*\*([^*\r\n]+?)\*\*")
_EXCLUSION = re.compile(
    r"\bexclu(?:ded|sions?|des)\b|\bnot (?:counted|included|a match|operative)\b|\bnon-?match|\bnear-?miss"
    r"|\bborderline\b|\bfalse positives?\b|\bdoes not\b"
)
_LEAD_IN_LEVEL = 7  # below any heading: a heading's section takes its lead-ins with it, not the other way round


def without_excluded_sections(text: str) -> str:
    """`text` without the sections its headings or bold lead-ins mark as rejected (`_EXCLUSION`): the marked line
    and everything to the next heading of the same or a higher level (for a heading) or to the next lead-in or
    heading (for a lead-in). The first section, with or without a heading, is never dropped: an answer that opens
    with its rejects is not an answer this rule can read, and it is left whole rather than emptied."""
    kept: list[str] = []
    dropping: int | None = None  # the level of the section being dropped
    first = True  # nothing kept yet: the section about to open is the answer's first
    for line in text.splitlines():
        heading, lead = _HEADING.match(line), _BOLD_LEAD.match(line)
        if heading or lead:
            level = len(heading.group(1)) if heading else _LEAD_IN_LEVEL
            label = heading.group(2) if heading else lead.group(1)
            if dropping is not None and level <= dropping:
                dropping = None
            if dropping is None and not first and _EXCLUSION.search(fold(label).lower()):
                dropping = level
        if line.strip():
            first = False
        if dropping is None:
            kept.append(line)
    return "\n".join(kept)


def named_contracts(
    text: str,
    universe: list[str],
    aliases: dict[str, list[str]] | None = None,
    doc_ids: dict[str, str] | None = None,
) -> set[str]:
    """Which items of `universe` an answer names, in its text: an item is named if its whole title appears, or if
    its filer's name appears and that filer has exactly one contract in the universe and a name of eight
    characters or more, or if one of its `aliases` appears: a word alias between word boundaries, a numeric alias
    as a number the answer states, to the value ("$1,200.80" does not state 20080; review of #812), or if its
    document id appears and `doc_ids` maps that id to its title. The synthetic key names an invoice by its file
    stem and aliases it by its invoice number and its total, where unique in the corpus; `corpora/synthetic_key.py`
    says which. The text is read without the sections the answer itself marks as rejected (a heading or bold
    lead-in saying "excluded", "not counted", "near-miss" and the like; `without_excluded_sections` has the whole
    list), so an item under "## False positive (excluded)" is not named, however it is named there. It misses an
    item referred to only by paraphrase ("the Papa John's deal"), and in running prose it still cannot tell a
    match from a non-match: "not included: `ebbb...`" in a sentence names that item as a title there would.

    `doc_ids` is document id -> title, built by the scoring site from the citations the answer's tools returned
    (`titles_by_doc_id` for a phase-4 response; a baseline's `cited_doc_ids` zipped with its `cited_doc_titles`).
    An id is read as a title is, squeezed to letters and digits, and the whole of it must appear: the eight-character
    floor is on the map's id, not a licence for partial matching, so a truncated id in the answer ("ebbbd23d-…")
    never resolves. An id the map does not hold, or one mapped to a title outside the universe, names nothing. Without the map a cloud model's
    list, which writes the parties and the id and never the filename stem, scored 0 named (2026-10-14).

    The answer's `citations` are otherwise deliberately not consulted. The app's citations field is every hit any
    tool call returned (`dedupe_citations(citations_accumulated)` in llm/chat.py: a median of 20 and up to 65 per
    list answer here), not what the answer refers to. Counting them would score search volume, and an answer
    saying "none" after one search would score like one that listed the hits (review of #697). The map only
    resolves ids the answer itself wrote out."""
    text = without_excluded_sections(text)
    squeezed, words = normalize(text), _words(text)
    amounts = amounts_in(text) if aliases else set()
    filers: dict[str, list[str]] = {}
    for title in universe:
        filers.setdefault(company(title), []).append(title)
    found = set()
    for title in universe:
        whole, filer = normalize(title), company(title)
        by_filer = len(filer) >= 8 and len(filers[filer]) == 1 and filer in squeezed
        by_alias = any(_mentions(words, amounts, alias) for alias in (aliases or {}).get(title, ()))
        if whole in squeezed or by_filer or by_alias:
            found.add(title)
    return found | _named_by_id(squeezed, universe, doc_ids)


def _named_by_id(squeezed: str, universe: list[str], doc_ids: dict[str, str] | None) -> set[str]:
    """The universe titles whose document id, as `doc_ids` maps it, is written in the answer (`squeezed` is the
    answer as `normalize` leaves it, so `ebbbd23d-3b0c-...` and `ebbbd23d3b0c...` are one id)."""
    if not doc_ids:
        return set()
    by_title = {normalize(title): title for title in universe}
    found = set()
    for doc_id, title in doc_ids.items():
        squeezed_id = normalize(str(doc_id or ""))
        if len(squeezed_id) >= 8 and squeezed_id in squeezed and (hit := by_title.get(normalize(str(title or "")))):
            found.add(hit)
    return found


def titles_by_doc_id(citations: Iterable | None) -> dict[str, str]:
    """The `doc_ids` map for `named_contracts` from a phase-4 response's `citations`, as the app returns them: each
    a dict with `doc_id` and `doc_title`, or carrying both under `source`. A citation without both says nothing,
    and a citation that is not a dict (an older response's bare id) says nothing either."""
    out: dict[str, str] = {}
    for citation in citations or ():
        if not isinstance(citation, dict):
            continue
        source = citation.get("source")
        if not isinstance(source, dict):
            source = {}
        doc_id = citation.get("doc_id") or source.get("doc_id")
        title = citation.get("doc_title") or source.get("doc_title")
        if doc_id and title:
            out[str(doc_id)] = str(title)
    return out


def mentions(text: str, alias: str) -> bool:
    """Whether `text` names `alias` as `named_contracts` would read it."""
    return _mentions(_words(text), amounts_in(text), alias)


def _mentions(words: str, amounts: set[Decimal], alias: str) -> bool:
    if re.fullmatch(r"\d+(?:\.\d+)?", alias):
        return Decimal(alias) in amounts
    form = normalize(alias)
    return bool(form) and re.search(rf"(?<![a-z0-9])(?:{_spaced(form)})(?![a-z0-9])", words) is not None


# ── counts ──

_UNITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")
_UNITS += ("thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_UNITS_FR = ("zero", "un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit", "neuf", "dix", "onze", "douze")
_UNITS_FR += ("treize", "quatorze", "quinze", "seize", "dix-sept", "dix-huit", "dix-neuf")
_TENS_FR = {"vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60}
NUMBER_WORDS: dict[str, int] = {w: i for i, w in enumerate(_UNITS)} | {w: i for i, w in enumerate(_UNITS_FR)}
NUMBER_WORDS |= _TENS | _TENS_FR
# "one", "un" and "une" are read only inside a compound ("twenty-one", "vingt-et-un"): on their own they are a
# pronoun and the French articles far more often than a number, and no key asks for a count of one.
_STANDALONE_WORDS = {w: n for w, n in NUMBER_WORDS.items() if n != 1}
_TENS_UNITS = re.compile(
    rf"\b({'|'.join(_TENS | _TENS_FR)})[ -](?:et[ -])?({'|'.join(_UNITS[1:10] + _UNITS_FR[1:10])})\b"
)
_NUMBER_WORD = re.compile(r"\b(" + "|".join(sorted(_STANDALONE_WORDS, key=len, reverse=True)) + r")\b")
# A month as a whole word, full or abbreviated, in either language ("nov", "sept", "juil"); not a prefix, so that
# "market 15" and "decided 5" keep their numbers.
_MONTH_WORD = "|".join(
    sorted(set(_MONTHS + _MONTHS_FR + _MONTHS_FR_SHORT + tuple(m[:3] for m in _MONTHS)), key=len, reverse=True)
)
_MONTH_DAY = re.compile(rf"\b(?:{_MONTH_WORD})\b\.?\s+\d{{1,2}}\b(?:,?\s*\d{{4}})?")
_DAY_MONTH = re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTH_WORD})\b\.?(?:,?\s*\d{{4}})?")
_MONTH_YEAR = re.compile(rf"\b(?:{_MONTH_WORD})\b\.?,?\s*(?:19|20)\d{{2}}\b")  # "Sept 2025" is a month, not seven
# Two months joined ("Jul–Sept", "July and Sept.", "juin au septembre") are months; so is "sept." with its full stop,
# which costs a French seven written "sept." at the end of a sentence (review of #812: "Sept" alone was seven).
_MONTH_SPAN = re.compile(rf"\b(?:{_MONTH_WORD})\b\.?\s*(?:[-–—]|to|and|et|au|a)\s*(?:{_MONTH_WORD})\b\.?")
_SEPT_ABBREVIATED = re.compile(r"\bsept\.")
_NUMERIC_DATE = re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b")
_RANGE = re.compile(r"\b\d+\s*[-–]\s*\d+\b")  # read after the years, so "2025 – 11 meetings" is not a range
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# A space inside a money amount is a thousands separator, so only before exactly three digits, and never a line
# break: "$275,926.50\n15 invoices" is an amount and then a count (review of #812).
_SP = r"[ \t\u00a0\u202f]"
_GROUPED = rf"\d(?:[\d,.]|{_SP}(?=\d{{3}}(?!\d)))*"
_MONEY = re.compile(rf"[$€£]{_SP}*{_GROUPED}|{_GROUPED}{_SP}*(?:[$€£]|\b(?:usd|cad|eur|dollars?)\b)")
# A list marker is a number at the start of a line with text after it on that line; "15." alone is an answer.
_LINE_MARKER = re.compile(r"(?m)^\s*(?:[-*•]|\d+[.)])[ \t]+(?=\S)")
# A standalone integer: digits, or digit groups of three with a comma, not touching a letter, an underscore, a
# colon or more digits through a separator ("0012_invoice", "9:00", "15.5", "141b4469" are not counts), not a
# percentage.
_INT = re.compile(r"(?<![\w,./:-])(\d{1,3}(?:,\d{3})+|\d+)(?![\w,.:]*\d)(?!\w)(?!\s?%)")


def counts_in(text: str) -> list[int]:
    """Every integer an answer states on its own, in the order stated, each once: as digits ("15", "1,234") or as a
    word up to ninety-nine in English or sixty-nine in French ("fifteen", "quinze", "vingt-cinq"). Read after
    stripping what carries digits without being a count: list markers at line starts (the fifteenth item of a list
    is not "15"), dates in any spelling, a month with a year or beside another month ("Sept 2025", "Jul–Sept" and
    "sept." are not seven), money, years, numeric ranges, decimals and percentages. What it cannot strip is stated:
    a day number beside a misspelt month survives, "one", "un" and "une" are not read on their own (see
    `_STANDALONE_WORDS`), and a French seven written "sept." at the end of a sentence is a month."""
    low = fold(text).lower()
    low = _LINE_MARKER.sub(" ", low)
    low = re.sub(r"(?<=\d)(st|nd|rd|th|er|re|e)\b", "", low)
    for pattern in (_MONTH_DAY, _DAY_MONTH, _MONTH_YEAR, _MONTH_SPAN, _SEPT_ABBREVIATED, _NUMERIC_DATE, _MONEY):
        low = pattern.sub(" ", low)
    low = _RANGE.sub(" ", _YEAR.sub(" ", low))
    found: list[tuple[int, int]] = [(m.start(), int(m.group(1).replace(",", ""))) for m in _INT.finditer(low)]
    compounds = list(_TENS_UNITS.finditer(low))  # "vingt-cinq" is 25, and neither 20 nor 5
    found += [(m.start(), NUMBER_WORDS[m.group(1)] + NUMBER_WORDS[m.group(2)]) for m in compounds]
    spans = [m.span() for m in compounds]
    for m in _NUMBER_WORD.finditer(low):
        if not any(a <= m.start() < b for a, b in spans):
            found.append((m.start(), _STANDALONE_WORDS[m.group(1)]))
    ordered: list[int] = []
    for _, value in sorted(found):
        if value not in ordered:
            ordered.append(value)
    return ordered


# ── amounts ──

_GROUP_SPACE = re.compile(r"(?<=\d)[   '](?=\d{3}(?!\d))")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def amounts_in(text: str) -> set[Decimal]:
    """Every number in an answer as a money amount, whichever way its digits are grouped: "364,107.38",
    "364 107,38", "364107.38", "2,580,000", "12.500" (a European thousand) and "12,500" are each one number.
    A separator followed by one or two digits at the end is the decimal mark; one followed by three is a
    thousands separator; two different separators make the last one the decimal mark. Currency symbols and codes
    are just not digits. Numbers that are not amounts (dates, invoice numbers) come along; they never equal a key
    by accident at the cent."""
    low = _GROUP_SPACE.sub("", fold(text))
    found: set[Decimal] = set()
    for token in _NUMBER.findall(low):
        value = _parse_amount(token)
        if value is not None:
            found.add(value)
    return found


def _parse_amount(token: str) -> Decimal | None:
    seps = [ch for ch in token if ch in ",."]
    if not seps:
        return Decimal(token)
    groups = re.split(r"[.,]", token)
    if len(set(seps)) == 2:  # 364,107.38 or 364.107,38: the last separator is the decimal mark
        decimal = seps[-1]
        head = token[: token.rfind(decimal)].replace(".", "").replace(",", "")
        return _decimal(f"{head}.{groups[-1]}")
    if len(seps) == 1 and len(groups[-1]) in (1, 2):
        return _decimal(f"{groups[0]}.{groups[1]}")
    if all(len(g) == 3 for g in groups[1:]):
        return Decimal("".join(groups))
    return None  # "1.2.3", "12,34,5": not a number this reads


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _has_amount(text: str, amount: float | int | str) -> bool:
    """`amount` is in the text to the cent, or rounded or truncated to the whole unit ("$275,926.50", "$275,927" and
    "$275,926" each state 275926.5; "$275,900" does not)."""
    want = Decimal(str(amount))
    whole = {want.to_integral_value(ROUND_HALF_UP), want.to_integral_value(ROUND_FLOOR)}
    return any(abs(found - want) < Decimal("0.005") or found in whole for found in amounts_in(text))


# ── the corpus a synthetic key was written for ──


def generation_id(facts_dir: Path) -> str:
    """The identity of a synthetic corpus generation: the first `GENERATION_DIGITS` hex digits of a SHA-256 over its
    fact sidecars, name and bytes, in name order. Raises FileNotFoundError when there is no sidecar there."""
    sidecars = sorted(Path(facts_dir).glob("*.json"))
    if not sidecars:
        raise FileNotFoundError(f"no fact sidecars in {facts_dir}")
    digest = hashlib.sha256()
    for sidecar in sidecars:
        digest.update(sidecar.name.encode())
        digest.update(b"\0")
        digest.update(sidecar.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:GENERATION_DIGITS]


_bound_root: Path | None = None
_generations: dict[Path, tuple[tuple, str]] = {}  # facts dir -> (what the sidecars looked like, their generation)


def bind_corpus_root(root: Path | str | None) -> None:
    """Say where the corpora are (the sweep's workdir, holding `synthetic/facts`). None unbinds; either way the
    generation read before is forgotten."""
    global _bound_root
    _bound_root = Path(root).expanduser() if root is not None else None
    _generations.clear()


def _sidecar_stats(facts: Path) -> tuple:
    """Name, size and mtime of every sidecar: cheap to read at every score, and a change in any of them is the cue
    to hash again, so a corpus regenerated under a run is caught (review of #812)."""
    return tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(facts.glob("*.json")))


def corpus_root() -> Path:
    """The workdir the synthetic corpus is read from: bound, `$HC_EVAL_WORKDIR`, this process's `--workdir`, or
    the harness default, in that order."""
    if _bound_root is not None:
        return _bound_root
    if env := os.environ.get(WORKDIR_ENV):
        return Path(env).expanduser()
    if argv := _workdir_from_argv():
        return argv
    from scripts.test_corpora import conftest as cfg

    return cfg.WORKDIR_DEFAULT


def _workdir_from_argv() -> Path | None:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--workdir")
    try:
        known, _ = parser.parse_known_args(sys.argv[1:])
    except SystemExit:  # `--workdir` with no value: not ours to complain about
        return None
    return Path(known.workdir).expanduser() if known.workdir else None


def check_generation(expected: str, corpus: str = "synthetic") -> None:
    """Refuse (`CorpusMismatch`) unless `<corpus_root()>/<corpus>/facts` is generation `expected`. The sidecars are
    hashed again whenever their names, sizes or mtimes changed since the last look."""
    root = corpus_root()
    facts = root / corpus / "facts"
    stats = _sidecar_stats(facts)
    cached = _generations.get(facts)
    try:
        actual = cached[1] if cached and cached[0] == stats else generation_id(facts)
    except FileNotFoundError:
        raise CorpusMismatch(
            f"the key is for {corpus} generation {expected}, but there is no {corpus} corpus under {root}. A keyed "
            "run does not generate one: acquire the corpus first with a run on the standard questions (no "
            "--questions-dir; --phases 0 is enough), then regenerate the key from it (corpora/synthetic_key.py "
            f"--snapshot --redo, a new PR), or put the corpus of generation {expected} there."
        ) from None
    _generations[facts] = (stats, actual)
    if actual != expected:
        raise CorpusMismatch(
            f"the key is for {corpus} generation {expected}; the {corpus} corpus under {root} is generation {actual}. "
            "Regenerate the key from this corpus (corpora/synthetic_key.py --snapshot --redo, a new PR) or score "
            "against the corpus the key was written for; a count is only true of the documents it counted."
        )


def refuse_foreign_keys(keys_by_corpus: dict[str, dict], corpora: Iterable[str], workdir: Path | str) -> None:
    """Before a run: bind `workdir`, and for every corpus in the run whose key carries a `_generation` header, check
    that the corpus under the workdir is that generation. Raises `CorpusMismatch` so the sweep can refuse to start,
    instead of dying at the first scored answer with the unit done and nothing to show for it (review of #812)."""
    bind_corpus_root(workdir)
    for corpus in corpora:
        header = (keys_by_corpus.get(corpus) or {}).get("_generation")
        if header:
            check_generation(header["generation"], header.get("corpus", corpus))


def score(entry: dict, answer: str, doc_ids: dict[str, str] | None = None) -> dict:
    """{"score": 0..1, ...detail}. An empty answer scores 0, and so does a fact, date, count or amount answer whose
    opening sentence says the thing is absent, before its text is searched (#713). "Absent" is any of quality.py's
    no-findings phrasings: the ones the baseline check reads anywhere ("no information about ...") and the ones
    read only in an opening, in English and French. An entry that names its corpus generation is first checked
    against the corpus under the workdir (`check_generation`). `doc_ids` (document id -> title, from the answer's
    citations) lets a list answer name an item by its id; see `named_contracts`. Without it, titles, filers and
    aliases are read as before."""
    answer = answer or ""
    kind = entry["kind"]
    if generation := entry.get("generation"):
        check_generation(generation)
    if kind in ("fact", "date", "count", "amount"):
        absent = nothing_found_signature(opening(answer), at_opening=True)
        if absent:
            groups = {"groups_found": 0, "groups": len(entry["all_of"])} if kind == "fact" else {}
            return {"score": 0.0, **groups, "absent": absent}
    if kind == "fact":
        squeezed = normalize(answer)
        hits = [any(normalize(alt) in squeezed for alt in group) for group in entry["all_of"]]
        return {"score": sum(hits) / len(hits), "groups_found": sum(hits), "groups": len(hits)}
    if kind == "date":
        found = _has_date(
            answer,
            entry["date"],
            day_first=bool(entry.get("day_first")),
            year_optional=bool(entry.get("year_optional")),
        )
        return {"score": 1.0 if found else 0.0}
    if kind == "count":
        found = counts_in(answer)
        return {"score": 1.0 if found and found[0] == int(entry["count"]) else 0.0, "numbers": found[:12]}
    if kind == "amount":
        return {"score": 1.0 if _has_amount(answer, entry["amount"]) else 0.0}
    if kind == "list":
        want = set(entry["positives"])
        got = named_contracts(answer, entry["universe"], entry.get("aliases"), doc_ids)
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
