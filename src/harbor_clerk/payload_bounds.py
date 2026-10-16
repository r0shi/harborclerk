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
one tool result gets 14,336 characters. ``kb_get_document`` carries a summary and a metadata
payload in one result, so both together stay under half of it (1,500 + 4,000), leaving the rest
for the title, jobs, source and the JSON indentation; ``tests/test_payload_bounds.py`` holds the
relation. A discriminator value is a hint for a ``metadata_filter`` (an exact match), and a value
longer than ``HINT_VALUE_MAX_CHARS`` is no usable filter value, so hints are bounded tighter.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from harbor_clerk.passage_cap import cap_text, is_structured_date

ELLIPSIS = "…"
TRUNCATED_KEY = "_truncated"

# The chat per-result budget at TIGHT_FIT_CONTEXT, the floor the limits below are sized against.
CHAT_RESULT_BUDGET_FLOOR_CHARS = 14_336
SUMMARY_MAX_CHARS = 1_500
METADATA_VALUE_MAX_CHARS = 1_000
METADATA_MAX_CHARS = 4_000
HINT_VALUE_MAX_CHARS = 200
HINT_MAX_CHARS = 1_000  # per candidate in kb_verify_identifier's discriminating_fields

# A dropped path longer than this is listed cut, so a pathological key cannot blow the list.
_PATH_MAX_CHARS = 120


def _json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _shorten(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(0, max_chars - 1)] + ELLIPSIS


def bound_summary(summary: str | None, cap: int | None, *, bounded: bool) -> str | None:
    """A document summary as a payload may carry it: the key's prefix (null at 0), then, when
    ``bounded``, at most ``SUMMARY_MAX_CHARS`` characters ending in ``…`` when cut."""
    if summary is None:
        return None
    text = cap_text(summary, cap)
    if text is None or not bounded:
        return text
    return _shorten(text, SUMMARY_MAX_CHARS)


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
    """``value`` with every string capped, then shortened to ``value_max`` (recorded in ``cut``)."""
    if isinstance(value, str):
        date = cap is not None and is_structured_date(dotted(path), value)
        text = value if date else cap_text(value, cap)
        if text is not None and value_max is not None and len(text) > value_max:
            cut.append(path)
            return _shorten(text, value_max)
        return text
    if isinstance(value, dict):
        return {k: _cap_tree(v, cap, value_max, (*path, str(k)), cut) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_cap_tree(v, cap, value_max, (*path, i), cut) for i, v in enumerate(value)]
    return value


def _fit_paths(paths: list[Path], room: int, describe: Callable[[Path], Any], more: Callable[[int], Any]) -> list[Any]:
    """As many of ``paths`` (described) as fit in ``room`` JSON characters, then a "more" marker."""
    out: list[Any] = []
    for i, path in enumerate(paths):
        item = describe(path)
        if isinstance(item, str):
            item = _shorten(item, _PATH_MAX_CHARS)
        rest = len(paths) - i - 1
        tail = [more(rest)] if rest else []
        if _json_len({TRUNCATED_KEY: [*out, item, *tail]}) + 1 > room:
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
    describe: Callable[[Path], Any] = dotted,
    more: Callable[[int], Any] = _more,
) -> dict | None:
    """A metadata dict as a payload may carry it (see the module docstring).

    The whole-payload bound works on entries two levels deep (``namespace.key``, the paths
    ``metadata_filter`` takes), kept in their stored order while they fit; a dropped entry's path
    is listed under ``_truncated`` with every shortened value's. The result's compact JSON is at
    most ``total_max`` characters, the ``_truncated`` list included. ``describe`` renders a path
    for the list (a dotted string by default) and ``more`` the marker for paths that did not fit.
    """
    if metadata is None:
        return None
    cut: list[Path] = []
    tree = _cap_tree(metadata, cap, value_max if bounded else None, (), cut)
    if not bounded:
        return tree
    if cut:
        whole = {**tree, TRUNCATED_KEY: [describe(p) for p in cut]}
        if _json_len(whole) <= total_max:
            return whole
    elif _json_len(tree) <= total_max:
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
            out[TRUNCATED_KEY] = _fit_paths(paths, total_max - _json_len(out), describe, more)
        return out

    # Greedy in stored order, against a budget that leaves room for the _truncated list; an entry
    # that does not fit is dropped and the next one is tried.
    budget = total_max - min(total_max // 4, 600)
    kept: list[Path] = []
    dropped: list[Path] = []
    size = 2
    for entry in entries:
        wrapped = {entry[0]: value_at(entry)} if len(entry) == 1 else {entry[0]: {entry[1]: value_at(entry)}}
        entry_len = _json_len(wrapped)
        if size + entry_len <= budget:
            kept.append(entry)
            size += entry_len
        else:
            dropped.append(entry)
    result = assemble(kept, dropped)
    while kept and _json_len(result) > total_max:  # the estimate above is close; this makes it exact
        dropped.insert(0, kept.pop())
        result = assemble(kept, dropped)
    return result
