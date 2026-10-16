# src/harbor_clerk/mcp_discriminator.py
"""Discriminator hint computation for kb_search responses.

When top-K hits span multiple docs whose scores are close AND whose
structured metadata fields differ, surface a hint that points the model
toward PR-F's metadata_filter for disambiguation. Pure post-processing
over the (hits, session) pair returned by hybrid_search.

The hint is OMITTED (not null) when any trigger condition fails — callers
should check `"discriminator_hint" in response` rather than truthiness.
"""

from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select

from harbor_clerk.models import Document
from harbor_clerk.payload_bounds import (
    HINT_VALUE_MAX_CHARS,
    METADATA_MAX_CHARS,
    SUGGESTION_MAX_CHARS,
    bound_metadata,
    cap_values,
    dotted,
    escaped_len,
    shorten,
)

# Internal namespace key written by the metadata extractor framework.
# Per-source timestamps always differ across docs, so excluding this
# namespace prevents it from drowning out real discriminating fields.
_PROVENANCE_KEY = "_source_provenance"

# Cap on how many differing fields appear in the hint output. Three is
# enough for a model to pick a useful filter without bloating the
# response. Ordered by distinctness (most discriminating first).
_MAX_FIELDS_IN_HINT = 3

# Cap on collection summaries embedded in differing_metadata. The full
# list/dict values per candidate can run thousands of chars (e.g.
# `sidecar.attendees` for 5 board-minutes docs is ~3 KB). The judge —
# and small-context models — don't need the full table; they need
# enough to recognize the field is non-scalar and pick a different
# discriminator. A length + first-element summary is sufficient.
_COLLECTION_SUMMARY_FIRST_CHARS = 60


async def _compute_discriminator_hint(hits, session, cap: int | None = None) -> dict | None:
    """Return a discriminator_hint dict if top hits are ambiguous on a
    structured metadata field, or None if no hint applies.

    `hits` is a list of SearchHit-shaped objects (must have `.doc_id`
    (str), `.doc_title` (str), `.score` (float)). `session` is a
    SQLAlchemy AsyncSession used for one SELECT against `documents.metadata`.
    `cap` is the calling key's snippet cap (``passage_cap.snippet_cap``):
    fields are compared, and every metadata string shown, as their first
    `cap` characters (#831). For every caller the values are then
    size-bounded (``payload_bounds``: ``HINT_VALUE_MAX_CHARS`` per value,
    ``METADATA_MAX_CHARS`` for `differing_metadata`, `_truncated` listing
    what was cut as `{field, doc_id}`). The suggestion names a filter only
    for a field whose values are all shown whole.
    """
    if len(hits) < 2:
        return None

    # Group hits by doc_id; keep the best score per doc.
    by_doc: dict[str, float] = {}
    titles: dict[str, str] = {}
    for h in hits:
        if h.doc_id not in by_doc or h.score > by_doc[h.doc_id]:
            by_doc[h.doc_id] = h.score
            titles[h.doc_id] = h.doc_title or h.doc_id

    if len(by_doc) < 2:
        return None  # all hits from a single doc

    # Candidate set: docs whose best score is within ε of the overall top.
    # Note: this additive ε differs from search.py's multiplicative `top_score
    # * 0.9` used for `possible_conflict`. The two thresholds are nearly
    # equivalent when top_score ≈ 1.0 (which is typical after the min-max
    # normalisation in search.py:_normalize_scores), but diverge slightly at
    # smaller top_scores. The additive form with a 0.05 floor protects against
    # over-narrowing when normalized scores are unusually low.
    top_score = max(by_doc.values())
    epsilon = max(0.05, 0.1 * top_score)
    candidates = [did for did, s in by_doc.items() if s >= top_score - epsilon]

    if len(candidates) < 2:
        return None
    # The hint is keyed by title; replies and copies share titles, so colliding ones are disambiguated
    # by doc id and every candidate keeps its own entry.
    labels = candidate_labels(titles, candidates)

    # Fetch metadata for the candidates (one indexed lookup).
    result = await session.execute(
        select(Document.doc_id, Document.doc_metadata).where(Document.doc_id.in_([uuid.UUID(c) for c in candidates]))
    )
    rows = result.all()
    metadata_by_doc: dict[str, dict] = {str(row.doc_id): (row.doc_metadata or {}) for row in rows}

    # Find paths where the candidates have differing values. Under a cap the values are compared as
    # the key sees them: two values that share their first `cap` characters are one value to it, and
    # calling them different would say they differ past the cap (#831).
    compared = (
        {did: cap_values(meta, cap) for did, meta in metadata_by_doc.items()} if cap is not None else metadata_by_doc
    )
    differing = _find_differing_metadata_fields(candidates, compared, labels)
    if not differing:
        return None

    # Order by: scalar fields first (so the suggestion can name a usable
    # metadata_filter value), then number of distinct values (most
    # discriminating first). A scalar field is something the LLM can put
    # back into a metadata_filter directly — strings, numbers, bools.
    # Lists/dicts get compacted in `differing_metadata` and are not
    # useful targets for the suggestion.
    def _sort_key(kv: tuple[str, dict[str, Any]]) -> tuple[int, int]:
        _, values_by_title = kv
        is_scalar = all(_is_scalar(v) for v in values_by_title.values())
        distinct = len({_make_hashable(v) for v in values_by_title.values()})
        # Lower tuple = higher priority. Scalar gets 0; non-scalar 1. Distinct
        # is negated so higher distinctness ranks first.
        return (0 if is_scalar else 1, -distinct)

    top_fields = sorted(differing.items(), key=_sort_key)[:_MAX_FIELDS_IN_HINT]

    # Compact non-scalar values so a list of 8 attendees doesn't expand to a
    # 3 KB block per doc. Scalar values pass through unchanged.
    compacted = {
        path: {t: _compact_value(v) for t, v in values_by_title.items()} for path, values_by_title in top_fields
    }

    # The key's cap (already applied above), then the size bound. A `_truncated` entry names the
    # field and the document by id: a title may contain dots, or be shared.
    doc_id_by_label = {label: did for did, label in labels.items()}

    def _describe(path: tuple) -> dict:
        out: dict[str, Any] = {"field": path[0]}
        if len(path) > 1:
            out["doc_id"] = doc_id_by_label.get(path[1], path[1])
        if len(path) > 2:
            out["at"] = dotted(path[2:])
        return out

    bounded = bound_metadata(
        compacted,
        None,  # the cap is already in `compared` (with its ISO-date exemption); only the size bound here
        bounded=True,
        value_max=HINT_VALUE_MAX_CHARS,
        total_max=METADATA_MAX_CHARS,
        depth=4,  # indent=2, inside kb_batch_search's results[i].discriminator_hint (kb_search nests it at 2)
        describe=_describe,
        more=lambda n: {"more": n},
    )
    # metadata_filter is an exact match on the whole stored value, so a field is offered as a filter only
    # when no candidate's value there is cut (by the bound or the cap), dropped by the size bound, or a
    # list/dict, and then only its whole values are quoted. "Whole" is decided against what
    # `differing_metadata` actually shows, so the hint and the suggestion cannot disagree. A candidate
    # holding null there neither blocks the field nor is offered.
    filterable: list[tuple[str, dict[str, Any]]] = []
    blocked: dict[str, str] = {}
    for path, _values in top_fields:
        shown = bounded.get(path)
        if not isinstance(shown, dict):
            blocked[path] = "dropped by the size bound"
            continue
        kinds = {
            did: _filter_kind(metadata_by_doc[did], compared[did], shown, labels[did], path)
            for did in candidates
            if did in metadata_by_doc
        }
        reasons = sorted(set(kinds.values()) - {"whole", "null"})
        if reasons:
            blocked[path] = " and ".join(reasons)
            continue
        offered = {labels[did]: _value_at(metadata_by_doc[did], path) for did, kind in kinds.items() if kind == "whole"}
        if offered:
            filterable.append((path, offered))

    return {
        "ambiguous_doc_ids": candidates,
        "ambiguous_doc_titles": [titles[d] for d in candidates],
        "differing_metadata": bounded,
        "suggestion": _build_suggestion(filterable, labels) if filterable else _no_filter_suggestion(blocked),
    }


def candidate_labels(titles: dict[str, str], doc_ids: list[str]) -> dict[str, str]:
    """A distinct label per candidate for keying per-document values: its title, or for a title two
    candidates share, the title plus the first 8 hex digits of its doc id ("Contract [3f2a1b9c]").
    Unique titles are left as they are, so the usual output shape does not change."""
    counts = Counter(titles[d] for d in doc_ids)
    labels = {d: titles[d] if counts[titles[d]] == 1 else f"{titles[d]} [{d[:8]}]" for d in doc_ids}
    if len(set(labels.values())) < len(labels):  # a prefix or a real title still collides: use the whole id
        labels = {d: titles[d] if counts[titles[d]] == 1 else f"{titles[d]} [{d}]" for d in doc_ids}
    return labels


def _value_at(metadata: dict, path: str) -> Any:
    ns, _, key = path.partition(".")
    ns_value = metadata.get(ns)
    return ns_value.get(key) if isinstance(ns_value, dict) else None


def _filter_kind(stored: dict, capped: dict, shown: dict, title: str, path: str) -> str:
    """How a candidate's stored value at ``path`` serves as a ``metadata_filter`` value, judged against
    ``shown``, the field's entry in the bounded ``differing_metadata`` ({title: value}), and ``capped``,
    the candidate's metadata as the key sees it: "null" (null or absent), "not filterable" (a dict, or a
    list holding one), "dropped by the size bound" (no entry for this candidate), "cut" (shown or seen
    differently from what is stored: the hint bound, in escaped characters, or the key's cap, which
    shows a string as null at 0), or "whole".

    A list of scalars is filterable (``_apply_jsonb_metadata_filter`` matches it with ``@>``). The hint
    shows it compacted, so it is whole when its entry is present, the key's cap left every element as
    stored, and no string element is past the hint bound."""
    value = _value_at(stored, path)
    if value is None:
        return "null"
    if isinstance(value, list):
        if not all(_is_scalar(v) for v in value):
            return "not filterable"
        if title not in shown:
            return "dropped by the size bound"
        too_long = any(isinstance(v, str) and escaped_len(v) > HINT_VALUE_MAX_CHARS for v in value)
        return "cut" if too_long or _value_at(capped, path) != value else "whole"
    if not _is_scalar(value):
        return "not filterable"
    if title not in shown:
        return "dropped by the size bound"
    if shown[title] != value:
        return "cut"
    return "whole"


def _no_filter_suggestion(blocked: dict[str, str]) -> str:
    fields = ", ".join(f"{path} ({reason})" for path, reason in blocked.items())
    text = (
        f"Top results are ambiguous; they differ on {fields}, so no value can be used as a metadata_filter, "
        "which needs a whole stored value (a scalar, or a list of scalars). Pin one document with doc_id instead."
    )
    # Field names are stored keys of any length; this text quotes no filter value, so a cut is safe.
    return shorten(text, SUGGESTION_MAX_CHARS)


def _is_scalar(v: Any) -> bool:
    """Treat strings, numbers, bools, and None as scalars. Lists/dicts/etc.
    are non-scalars and get compacted before reaching the LLM."""
    return v is None or isinstance(v, str | int | float | bool)


def _compact_value(v: Any) -> Any:
    """Return a JSON-serializable summary of v that is small even when v
    itself is a large list/dict. Scalars pass through.

    For lists: ``{"len": N, "first": str(v[0])[:60]}`` so the LLM can see
    the field is multi-valued and recognize the type without paying the
    serialization cost of every element.

    For dicts: ``{"keys": sorted(v.keys())[:5], "len": N}`` — keys are
    usually short and distinctive; values can be arbitrarily large.

    For anything else non-scalar (sets, custom objects): stringify and
    truncate.
    """
    if _is_scalar(v):
        return v
    if isinstance(v, list):
        out: dict[str, Any] = {"len": len(v)}
        if v:
            out["first"] = str(v[0])[:_COLLECTION_SUMMARY_FIRST_CHARS]
        return out
    if isinstance(v, dict):
        keys = sorted(str(k) for k in v)
        return {"len": len(v), "keys": keys[:5]}
    return str(v)[:_COLLECTION_SUMMARY_FIRST_CHARS]


def _find_differing_metadata_fields(
    candidates: list[str],
    metadata_by_doc: dict[str, dict],
    titles: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """For each `<namespace>.<key>` path present in ALL candidates' metadata,
    return a {title: value} mapping if values differ. Skips _source_provenance.
    Output: {"sidecar.vendor": {"A": "X", "B": "Y"}, ...}.
    """
    # Collect all paths that any candidate has, grouped by candidate
    paths_by_doc: dict[str, set[tuple[str, str]]] = {}
    for did in candidates:
        meta = metadata_by_doc.get(did, {})
        paths_by_doc[did] = set()
        for namespace, ns_dict in meta.items():
            if namespace == _PROVENANCE_KEY:
                continue
            if not isinstance(ns_dict, dict):
                continue
            for key in ns_dict:
                paths_by_doc[did].add((namespace, key))

    # Paths that exist in EVERY candidate
    if not paths_by_doc:
        return {}
    common_paths = set.intersection(*paths_by_doc.values())

    # For each common path, collect the per-doc values; keep only paths where
    # values differ (more than 1 distinct value).
    differing: dict[str, dict[str, Any]] = {}
    for namespace, key in common_paths:
        per_title: dict[str, Any] = {}
        for did in candidates:
            value = metadata_by_doc[did][namespace][key]
            per_title[titles[did]] = value
        if len({_make_hashable(v) for v in per_title.values()}) > 1:
            differing[f"{namespace}.{key}"] = per_title

    return differing


def _make_hashable(v: Any) -> Any:
    """Convert v to a hashable form for set-based distinctness checks.
    Lists/dicts/etc are converted to tuples/frozensets; primitives pass through."""
    if isinstance(v, list):
        return tuple(_make_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _make_hashable(vv)) for k, vv in v.items()))
    return v


def _build_suggestion(top_fields: list[tuple[str, dict[str, Any]]], titles: dict[str, str]) -> str:
    """Construct a one-line human-readable suggestion for the model.

    Picks the most discriminating field (first in top_fields) and surfaces
    one concrete metadata_filter call. Prefers a value that uniquely pins
    a single doc when one exists; otherwise falls back to the first-iterated
    value and softens the claim to "narrow results".

    The whole string stays within ``SUGGESTION_MAX_CHARS`` escaped characters: "Available values"
    lists as many candidates as fit and says how many it left out, and a filter too long to quote at
    all becomes a no-filter suggestion.
    """
    if not top_fields:
        return "Top results are ambiguous but no discriminating metadata fields found."

    path, values_by_title = top_fields[0]

    # Count how often each value appears across candidates to prefer one that
    # uniquely pins a single doc.
    value_counts: dict[Any, int] = {}
    for v in values_by_title.values():
        key = _make_hashable(v)
        value_counts[key] = value_counts.get(key, 0) + 1

    # Pick a value that appears exactly once if possible
    unique_value = next(
        (v for v in values_by_title.values() if value_counts[_make_hashable(v)] == 1),
        None,
    )
    if unique_value is not None:
        head = f"Top results are ambiguous. Use metadata_filter={{'{path}': {unique_value!r}}} to pin one doc."
    else:
        # No value uniquely pins → soften the claim
        first_value = next(iter(values_by_title.values()))
        head = f"Top results are ambiguous. Use metadata_filter={{'{path}': {first_value!r}}} to narrow results."

    def compose(listed: list[str], omitted: int) -> str:
        more = f"{', ' if listed else ''}{omitted} more not listed" if omitted else ""
        return f"{head} Available values: {', '.join(listed)}{more}."

    if escaped_len(compose([], len(values_by_title))) > SUGGESTION_MAX_CHARS:
        return _no_filter_suggestion({path: "filter too long to quote"})
    items = [f"{t}={v!r}" for t, v in values_by_title.items()]
    listed: list[str] = []
    for i, item in enumerate(items):
        rest = len(items) - i - 1
        if escaped_len(compose([*listed, item], rest)) > SUGGESTION_MAX_CHARS:
            return compose(listed, len(items) - i)
        listed.append(item)
    return compose(listed, 0)
