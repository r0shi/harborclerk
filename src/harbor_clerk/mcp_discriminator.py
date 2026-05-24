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
from typing import Any

from sqlalchemy import select

from harbor_clerk.models import Document

# Internal namespace key written by the metadata extractor framework.
# Per-source timestamps always differ across docs, so excluding this
# namespace prevents it from drowning out real discriminating fields.
_PROVENANCE_KEY = "_source_provenance"

# Cap on how many differing fields appear in the hint output. Three is
# enough for a model to pick a useful filter without bloating the
# response. Ordered by distinctness (most discriminating first).
_MAX_FIELDS_IN_HINT = 3


def _compute_discriminator_hint(hits, session) -> dict | None:
    """Return a discriminator_hint dict if top hits are ambiguous on a
    structured metadata field, or None if no hint applies.

    `hits` is a list of SearchHit-shaped objects (must have `.doc_id`
    (str), `.doc_title` (str), `.score` (float)). `session` is a
    SQLAlchemy session used for one SELECT against `documents.metadata`.
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
    top_score = max(by_doc.values())
    epsilon = max(0.05, 0.1 * top_score)
    candidates = [did for did, s in by_doc.items() if s >= top_score - epsilon]

    if len(candidates) < 2:
        return None

    # Fetch metadata for the candidates (one indexed lookup).
    rows = session.execute(
        select(Document.doc_id, Document.doc_metadata).where(Document.doc_id.in_([uuid.UUID(c) for c in candidates]))
    ).all()
    metadata_by_doc: dict[str, dict] = {str(row.doc_id): (row.doc_metadata or {}) for row in rows}

    # Find paths where the candidates have differing values.
    differing = _find_differing_metadata_fields(candidates, metadata_by_doc, titles)
    if not differing:
        return None

    # Order by number of distinct values (most discriminating first).
    top_fields = sorted(
        differing.items(),
        key=lambda kv: -len({_make_hashable(v) for v in kv[1].values()}),
    )[:_MAX_FIELDS_IN_HINT]

    return {
        "ambiguous_doc_ids": candidates,
        "ambiguous_doc_titles": [titles[d] for d in candidates],
        "differing_metadata": dict(top_fields),
        "suggestion": _build_suggestion(top_fields, titles),
    }


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
    one concrete metadata_filter call. Falls back gracefully if top_fields
    is empty (shouldn't happen — the caller checks first).
    """
    if not top_fields:
        return "Top results are ambiguous but no discriminating metadata fields found."

    path, values_by_title = top_fields[0]
    # Pick the first concrete value to suggest.
    first_value = next(iter(values_by_title.values()))
    return (
        f"Top results are ambiguous. Use metadata_filter={{'{path}': {first_value!r}}} "
        f"to pin one doc. Available values: " + ", ".join(f"{t}={v!r}" for t, v in values_by_title.items()) + "."
    )
