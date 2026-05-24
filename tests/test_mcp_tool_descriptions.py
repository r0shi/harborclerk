"""Grep-style assertions that the rewritten kb_* tool descriptions hit
the required surface. These pin the descriptions against future drift —
if a future PR removes a key behavior cue, the regression is loud."""

from harbor_clerk.mcp_server import (
    kb_batch_search,
    kb_corpus_overview,
    kb_document_outline,
    kb_expand_context,
    kb_find_related,
    kb_get_document,
    kb_read_passages,
    kb_search,
)


def _doc(fn):
    return (fn.__doc__ or "").lower()


# ── Major tier ────────────────────────────────────────────────────────


def test_kb_search_description_mentions_metadata_filter():
    assert "metadata_filter" in _doc(kb_search)


def test_kb_search_description_mentions_discriminator_hint():
    assert "discriminator_hint" in _doc(kb_search)


def test_kb_search_description_mentions_has_more_iteration():
    d = _doc(kb_search)
    assert "has_more" in d
    # Should mention what to do with it — paginate or refine
    assert "paginate" in d or "refine" in d or "iterate" in d


def test_kb_search_description_has_explicit_decline_guidance():
    """The negative-hedging fix lives in this description — must explicitly
    instruct the model to decline cleanly when no match exists."""
    d = _doc(kb_search)
    # Multiple phrasings acceptable — pin the intent, not the exact words
    assert any(
        cue in d
        for cue in (
            "not in the corpus",
            "doesn't exist",
            "say so",
            "decline",
            "no relevant",
        )
    )


def test_kb_batch_search_description_mentions_preference_over_sequential():
    """The under-tooling fix: gpt-4o doesn't reach for batch_search unless
    told to. Description must explicitly recommend it over multiple
    sequential kb_search calls."""
    d = _doc(kb_batch_search)
    assert "kb_search" in d
    # Some form of "prefer over sequential calls"
    assert any(cue in d for cue in ("prefer", "instead of", "rather than", "use this over"))


def test_kb_read_passages_description_recommends_verify_before_answer():
    """The verify-before-claim pattern from the negative-hedging items."""
    d = _doc(kb_read_passages)
    assert "verify" in d or "confirm" in d or "before answering" in d


def test_kb_get_document_description_mentions_metadata_field():
    """kb_get_document's response includes the metadata dict — description
    must surface that as the way to discover metadata_filter keys."""
    d = _doc(kb_get_document)
    assert "metadata" in d
    assert "metadata_filter" in d or "filter keys" in d


# ── Moderate tier ─────────────────────────────────────────────────────


def test_kb_expand_context_description_recommends_pairing_with_read_passages():
    d = _doc(kb_expand_context)
    assert "kb_read_passages" in d


def test_kb_find_related_description_explains_when_it_beats_kb_search():
    d = _doc(kb_find_related)
    # Should explain it's a complement: you have one good hit and want to
    # expand the relevance set
    assert "expand" in d or "complement" in d or "related" in d


def test_kb_document_outline_description_recommends_pairing_with_read_passages():
    d = _doc(kb_document_outline)
    assert "kb_read_passages" in d


def test_kb_corpus_overview_description_recommends_first_use():
    """kb_corpus_overview should be flagged as the right FIRST call when
    the model doesn't know the corpus shape."""
    d = _doc(kb_corpus_overview)
    assert "first" in d or "start" in d
