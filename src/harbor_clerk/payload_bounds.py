"""Derived document text in a tool payload: the snippet cap, then a size bound (#831).

Summaries and metadata are document text as an operator means it: a Tika ``description``, a
frontmatter field or a sidecar JSON value can quote the body, and a sidecar can be 64 KB. Two
rules apply to them, in this order:

1. **The snippet cap** (``passage_cap``), for a capped API key only: every string value is cut to
   its first ``cap`` characters, null at a cap of 0, exactly as a passage is (no ellipsis: a capped
   passage carries none either). Non-string scalars (numbers, booleans) and key names are kept.
2. **The size bound**, for every MCP/tool payload (an API key, an OAuth agent, the human's own
   chat and research tools, which put the payload in a local model's context) and for every
   non-human REST principal; a human's web-app request over REST is unbounded. A summary longer
   than ``SUMMARY_MAX_CHARS`` and a metadata string longer than its per-value bound end in ``…``;
   a metadata payload whose JSON is longer than its whole-payload bound keeps the leading entries
   that fit and drops the rest. Every shortened or dropped path is listed, dotted, under
   ``"_truncated"`` in the bounded dict itself, so truncation is never silent.

The limits come from the chat per-result budget (``llm.chat._tool_result_max_chars``): at the
tight-fit context every curated model can run at (``llm.models.TIGHT_FIT_CONTEXT``, 16,384 tokens)
one tool result gets 14,336 characters, measured on the string the tool returns. So every bound
counts in that unit: escaped JSON (``json.dumps`` writes each non-ASCII character as a 6-character
``\\uXXXX``, so a CJK or accented value costs up to six times its length) with the indentation of
the response (``_json_len``). ``kb_get_document`` carries a summary and a metadata payload in one
result, so both together stay under half of the budget (1,500 + 4,000), leaving the rest for the
title, jobs and source; ``tests/test_key_snippet_cap.py`` checks a worst case end to end, in CJK
and in accented French, as the chat tool returns it. The bounds are per item: a list tool
(``kb_list_recent``, ``kb_corpus_overview``, ``kb_find_related``, ``kb_verify_identifier``) can exceed
one result with many rows, and the chat's ``_truncate_for_llm`` then drops whole rows from its array
(``_LARGE_ARRAY_KEYS``) rather than cutting mid-JSON. A discriminator value is a hint for a ``metadata_filter`` (an exact match), and a value
longer than ``HINT_VALUE_MAX_CHARS`` is no usable filter value, so hints are bounded tighter.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from harbor_clerk.passage_cap import cap_text, date_path_of, is_mail_header, is_structured_date

ELLIPSIS = "…"
TRUNCATED_KEY = "_truncated"

# The chat per-result budget at TIGHT_FIT_CONTEXT, the floor the limits below are sized against.
CHAT_RESULT_BUDGET_FLOOR_CHARS = 14_336
SUMMARY_MAX_CHARS = 1_500
METADATA_VALUE_MAX_CHARS = 1_000
METADATA_MAX_CHARS = 4_000
HINT_VALUE_MAX_CHARS = 200
HINT_MAX_CHARS = 1_000  # per candidate in kb_verify_identifier's discriminating_fields
# The discriminator's suggestion quotes one filter value and lists the candidates' values: room for
# about four hint values plus the fixed text, in escaped characters.
SUGGESTION_MAX_CHARS = 4 * HINT_VALUE_MAX_CHARS + 400

# A dropped path longer than this is listed cut, so a pathological key cannot blow the list.
_PATH_MAX_CHARS = 120

# A stored top-level key that would collide with the marker is renamed to this on output.
STORED_TRUNCATED_KEY = "_truncated (stored)"

_INDENT = 2


def _json_len(value: Any, depth: int | None = None) -> int:
    """Characters ``value`` takes in a tool response, in the unit the chat budget measures.

    The tools send ``json.dumps``' default ASCII escaping (every non-ASCII character is a 6-character
    ``\\uXXXX``, 12 for one outside the BMP), so that is what is counted. ``depth`` is how many
    containers enclose the value in a response sent with ``indent=2`` (``kb_get_document``'s
    ``metadata`` is 1): each line after the first then carries two spaces per level. ``None`` is a
    response sent without indentation (``json.dumps(result)``: ", " and ": " separators)."""
    if depth is None:
        return len(json.dumps(value, default=str))
    text = json.dumps(value, indent=_INDENT, default=str)
    return len(text) + text.count("\n") * _INDENT * depth


def _escaped_len(text: str) -> int:
    return len(json.dumps(text)) - 2


def _shorten(text: str, max_chars: int, *, escaped: bool = True) -> str:
    """``text`` within ``max_chars`` characters, ``…`` when cut. ``escaped`` counts as an MCP or chat
    tool response carries it (escaped JSON); otherwise code points, as a REST response does."""
    size = _escaped_len if escaped else len
    if size(text) <= max_chars:
        return text
    budget = max_chars - size(ELLIPSIS)
    used = 0
    end = 0
    for i, ch in enumerate(text):
        cost = size(ch)
        if used + cost > budget:
            break
        used += cost
        end = i + 1
    return text[:end] + ELLIPSIS


def escaped_len(text: str) -> int:
    """Characters ``text`` takes as a JSON string in a tool response, quotes excluded."""
    return _escaped_len(text)


def shorten(text: str, max_chars: int) -> str:
    """``text`` within ``max_chars`` escaped characters, ending in ``…`` when cut."""
    return _shorten(text, max_chars)


def bound_summary(summary: str | None, cap: int | None, *, bounded: bool, escaped: bool = True) -> str | None:
    """A document summary as a payload may carry it: the key's prefix (null at 0), then, when
    ``bounded``, at most ``SUMMARY_MAX_CHARS`` characters, ending in ``…`` when cut. The unit is the
    surface's: escaped JSON for an MCP or in-process tool (``escaped``), code points for REST, whose
    JSON responses are UTF-8 unescaped."""
    if summary is None:
        return None
    text = cap_text(summary, cap)
    if text is None or not bounded:
        return text
    return _shorten(text, SUMMARY_MAX_CHARS, escaped=escaped)


# A path into a metadata tree: dict keys and list indices, outermost first.
Path = tuple[str | int, ...]


def dotted(path: Path) -> str:
    """``("sidecar", "parties", 0, "name")`` as ``sidecar.parties[0].name``, the ``_truncated`` form."""
    out = ""
    for seg in path:
        out += f"[{seg}]" if isinstance(seg, int) else (f".{seg}" if out else str(seg))
    return out


def _more(n: int) -> str:
    return f"+{n} more"


def cap_values(value: Any, cap: int | None) -> Any:
    """``value`` with every string cut to the key's prefix (null at 0), ISO dates in the date
    fields excepted (``passage_cap.DATE_FIELD_PATHS``); nothing else changes."""
    return _cap_tree(value, cap, None, (), [])


def _cap_tree(value: Any, cap: int | None, value_max: int | None, path: Path, cut: list[Path]) -> Any:
    """``value`` with every string capped, then shortened to ``value_max`` escaped characters
    (recorded in ``cut``)."""
    if isinstance(value, str):
        # The same notion of "directly on a date field" as the filter exemption (scalar or list element,
        # never a dict child): ``passage_cap.date_path_of``.
        # Dates and Tika mail headers directly on their field are outside the cap (``passage_cap``).
        date = cap is not None and (is_structured_date(date_path_of(path), value) or is_mail_header(path))
        text = value if date else cap_text(value, cap)
        if text is not None and value_max is not None and _escaped_len(text) > value_max:
            cut.append(path)
            return _shorten(text, value_max)
        return text
    if isinstance(value, dict):
        return {k: _cap_tree(v, cap, value_max, (*path, str(k)), cut) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_cap_tree(v, cap, value_max, (*path, i), cut) for i, v in enumerate(value)]
    return value


def _fit_paths(
    base: dict,
    paths: list[Path],
    fits: Callable[[dict], bool],
    describe: Callable[[Path], Any],
    more: Callable[[int], Any],
) -> list[Any]:
    """As many of ``paths`` (described) as keep ``base`` plus the list within the bound, then a
    "more" marker; measured on the assembled dict, so the result is exact."""
    out: list[Any] = []
    for i, path in enumerate(paths):
        item = describe(path)
        if isinstance(item, str):
            item = _shorten(item, _PATH_MAX_CHARS)
        rest = len(paths) - i - 1
        tail = [more(rest)] if rest else []
        if not fits({**base, TRUNCATED_KEY: [*out, item, *tail]}):
            return [*out, more(len(paths) - i)]
        out.append(item)
    return out


def bound_metadata(
    metadata: dict | None,
    cap: int | None,
    *,
    bounded: bool,
    value_max: int = METADATA_VALUE_MAX_CHARS,
    total_max: int = METADATA_MAX_CHARS,
    depth: int | None = None,
    describe: Callable[[Path], Any] = dotted,
    more: Callable[[int], Any] = _more,
) -> dict | None:
    """A metadata dict as a payload may carry it (see the module docstring).

    The whole-payload bound works on entries two levels deep (``namespace.key``, the paths
    ``metadata_filter`` takes), kept in their stored order while they fit; a dropped entry's path
    is listed under ``_truncated`` with every shortened value's. All sizes are in the response's own
    unit (``_json_len``: escaped JSON, indented as ``depth`` says), so the result takes at most
    ``total_max`` characters where it is sent, the ``_truncated`` list included. A stored top-level
    key named ``_truncated`` is renamed ``STORED_TRUNCATED_KEY``. ``describe`` renders a path for the
    list (a dotted string by default) and ``more`` the marker for paths that did not fit.
    """
    if metadata is None:
        return None
    if bounded and TRUNCATED_KEY in metadata:
        metadata = {(STORED_TRUNCATED_KEY if k == TRUNCATED_KEY else k): v for k, v in metadata.items()}
    cut: list[Path] = []
    tree = _cap_tree(metadata, cap, value_max if bounded else None, (), cut)
    if not bounded:
        return tree

    def fits(candidate: dict) -> bool:
        return _json_len(candidate, depth) <= total_max

    if cut:
        whole = {**tree, TRUNCATED_KEY: [describe(p) for p in cut]}
        if fits(whole):
            return whole
    elif fits(tree):
        return tree

    # Entries: (namespace, key) for a dict namespace, (namespace,) for anything else.
    entries: list[Path] = []
    for ns, ns_value in tree.items():
        if isinstance(ns_value, dict) and ns_value:
            entries.extend((ns, key) for key in ns_value)
        else:
            entries.append((ns,))

    def value_at(entry: Path) -> Any:
        return tree[entry[0]] if len(entry) == 1 else tree[entry[0]][entry[1]]

    def assemble(kept: list[Path], dropped: list[Path]) -> dict:
        out: dict[str, Any] = {}
        for entry in kept:
            if len(entry) == 1:
                out[entry[0]] = tree[entry[0]]
            else:
                out.setdefault(entry[0], {})[entry[1]] = value_at(entry)
        # A shortened value inside a dropped entry is reported by the entry's own path.
        shortened = [p for p in cut if not any(p[: len(d)] == d for d in dropped)]
        paths = shortened + dropped
        if paths:
            out[TRUNCATED_KEY] = _fit_paths(out, paths, fits, describe, more)
        return out

    # Greedy in stored order, against a budget that leaves room for the _truncated list; an entry
    # that does not fit is dropped and the next one is tried.
    budget = total_max - min(total_max // 4, 600)
    kept: list[Path] = []
    dropped: list[Path] = []
    size = 2
    for entry in entries:
        wrapped = {entry[0]: value_at(entry)} if len(entry) == 1 else {entry[0]: {entry[1]: value_at(entry)}}
        entry_len = _json_len(wrapped, depth)
        if size + entry_len <= budget:
            kept.append(entry)
            size += entry_len
        else:
            dropped.append(entry)
    result = assemble(kept, dropped)
    while kept and not fits(result):  # the estimate above is close; this makes it exact
        dropped.insert(0, kept.pop())
        result = assemble(kept, dropped)
    return result
