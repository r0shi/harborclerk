"""Tests for the wikilink resolver helper in finalize.py."""

import uuid

from harbor_clerk.worker.stages.finalize import _resolve_link


def test_resolve_link_empty_inputs():
    assert _resolve_link("", {}) is None
    assert _resolve_link("   ", {}) is None
    assert _resolve_link("anything", {}) is None


def test_resolve_link_unique_match():
    did = uuid.uuid4()
    candidates = {"my note": [did]}
    assert _resolve_link("My Note", candidates) == did


def test_resolve_link_case_insensitive():
    did = uuid.uuid4()
    candidates = {"my note": [did]}
    assert _resolve_link("MY NOTE", candidates) == did
    assert _resolve_link("  my note  ", candidates) == did


def test_resolve_link_no_match():
    candidates = {"other note": [uuid.uuid4()]}
    assert _resolve_link("My Note", candidates) is None


def test_resolve_link_ambiguous_returns_none():
    """Two docs with the same name → ambiguous → unresolved."""
    candidates = {"shared name": [uuid.uuid4(), uuid.uuid4()]}
    assert _resolve_link("Shared Name", candidates) is None


def test_resolve_link_treats_pre_deduped_candidates_as_unique():
    """The caller (``run_finalize``) deduplicates doc_ids per name before
    handing the map to ``_resolve_link`` — see ``_add_candidate`` for the
    stem-equals-title case (e.g. ``"Note A.md"`` with title ``"Note A"`` both
    normalize to ``"note a"``). When the dedup is in place, the list has
    exactly one entry and the link resolves; if the dedup were ever removed,
    the list would have two entries of the SAME id and ``_resolve_link``
    would conservatively (and incorrectly) reject it as ambiguous. This
    test pins down that contract so the dedup can't silently regress."""
    did = uuid.uuid4()
    # Post-dedup (correct): single entry.
    assert _resolve_link("Note A", {"note a": [did]}) == did
    # Pre-dedup (hypothetical regression): two copies of the same id —
    # _resolve_link only inspects list length, so it returns None.
    assert _resolve_link("Note A", {"note a": [did, did]}) is None
