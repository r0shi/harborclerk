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
from typing import Any

from harbor_clerk.passage_cap import cap_text

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


def _cap_tree(value: Any, cap: int | None, value_max: int | None, path: str, cut: list[str]) -> Any:
    """``value`` with every string capped, then shortened to ``value_max`` (recorded in ``cut``)."""
    if isinstance(value, str):
        text = cap_text(value, cap)
        if text is not None and value_max is not None and len(text) > value_max:
            cut.append(path)
            return _shorten(text, value_max)
        return text
    if isinstance(value, dict):
        return {k: _cap_tree(v, cap, value_max, f"{path}.{k}" if path else str(k), cut) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_cap_tree(v, cap, value_max, f"{path}[{i}]", cut) for i, v in enumerate(value)]
    return value


def _fit_paths(paths: list[str], room: int) -> list[str]:
    """As many of ``paths`` as fit in ``room`` JSON characters, then a "+N more" marker."""
    out: list[str] = []
    for i, path in enumerate(paths):
        path = _shorten(path, _PATH_MAX_CHARS)
        rest = len(paths) - i - 1
        tail = [f"+{rest} more"] if rest else []
        if _json_len({TRUNCATED_KEY: [*out, path, *tail]}) + 1 > room:
            return [*out, f"+{len(paths) - i} more"]
        out.append(path)
    return out


def bound_metadata(
    metadata: dict | None,
    cap: int | None,
    *,
    bounded: bool,
    value_max: int = METADATA_VALUE_MAX_CHARS,
    total_max: int = METADATA_MAX_CHARS,
) -> dict | None:
    """A metadata dict as a payload may carry it (see the module docstring).

    The whole-payload bound works on entries two levels deep (``namespace.key``, the paths
    ``metadata_filter`` takes), kept in their stored order while they fit; a dropped entry's path
    is listed under ``_truncated`` with every shortened value's. The result's compact JSON is at
    most ``total_max`` characters, the ``_truncated`` list included.
    """
    if metadata is None:
        return None
    cut: list[str] = []
    tree = _cap_tree(metadata, cap, value_max if bounded else None, "", cut)
    if not bounded:
        return tree
    whole = {**tree, TRUNCATED_KEY: cut} if cut else tree
    if _json_len(whole) <= total_max:
        return whole

    # Entries: (namespace, key) for a dict namespace, (namespace, None) for anything else.
    entries: list[tuple[str, str | None]] = []
    for ns, ns_value in tree.items():
        if isinstance(ns_value, dict) and ns_value:
            entries.extend((ns, key) for key in ns_value)
        else:
            entries.append((ns, None))

    def path_of(entry: tuple[str, str | None]) -> str:
        ns, key = entry
        return ns if key is None else f"{ns}.{key}"

    def assemble(kept: list[tuple[str, str | None]], dropped: list[tuple[str, str | None]]) -> dict:
        out: dict[str, Any] = {}
        for ns, key in kept:
            if key is None:
                out[ns] = tree[ns]
            else:
                out.setdefault(ns, {})[key] = tree[ns][key]
        dropped_paths = [path_of(e) for e in dropped]
        # A shortened value inside a dropped entry is reported by the entry's own path.
        shortened = [p for p in cut if not any(p == d or p.startswith((f"{d}.", f"{d}[")) for d in dropped_paths)]
        paths = shortened + dropped_paths
        if paths:
            out[TRUNCATED_KEY] = _fit_paths(paths, total_max - _json_len(out))
        return out

    # Greedy in stored order, against a budget that leaves room for the _truncated list; an entry
    # that does not fit is dropped and the next one is tried.
    budget = total_max - min(total_max // 4, 600)
    kept: list[tuple[str, str | None]] = []
    dropped: list[tuple[str, str | None]] = []
    size = 2
    for entry in entries:
        ns, key = entry
        entry_len = _json_len({ns: tree[ns]} if key is None else {ns: {key: tree[ns][key]}})
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
