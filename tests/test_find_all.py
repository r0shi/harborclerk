"""Unit tests for find_all() — doc-level enumeration."""

import datetime as dt

import pytest

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import find_all


async def _seed_docs_with_chunks(db_session, n_docs: int, chunks_per_doc: int, text: str = "off-balance-sheet entity"):
    """Seed n_docs documents, each with chunks_per_doc chunks all containing `text`."""
    doc_ids = []
    for i in range(n_docs):
        sha = f"sha_seed_{i:020d}".encode()
        doc = Document(
            title=f"Doc {i}", status="active", sha256=sha, pipeline_status=PipelineStatus.ready, mime_type="text/plain"
        )
        db_session.add(doc)
        await db_session.flush()
        for j in range(chunks_per_doc):
            db_session.add(
                Chunk(doc_id=doc.doc_id, chunk_num=j, chunk_text=f"{text} chunk {j} of doc {i}", language="en")
            )
        doc_ids.append(doc.doc_id)
    await db_session.flush()
    return doc_ids


async def test_find_all_dedupes_by_doc_id(db_session):
    """A doc with 5 matching chunks shows up exactly ONCE in results."""
    doc_ids = await _seed_docs_with_chunks(db_session, n_docs=3, chunks_per_doc=5)

    result = await find_all(db_session, "off-balance-sheet", max_results=10)

    assert len(result.hits) == 3, f"expected 3 docs, got {len(result.hits)}"
    assert {h.doc_id for h in result.hits} == {str(d) for d in doc_ids}
    assert result.total_matches == 3
    assert result.truncated is False


async def test_find_all_total_matches_unaffected_by_max_results(db_session):
    """total_matches reflects ALL matches, even when max_results truncates.

    Uses max_results=5 so internal_k=(0+5)*5=25 comfortably covers all
    10 docs × 1 chunk — avoids the FTS candidate-pool starvation that
    occurs with max_results=3 (internal_k=15) across 10 docs × 2 chunks.
    """
    await _seed_docs_with_chunks(db_session, n_docs=10, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=5)

    assert len(result.hits) == 5
    assert result.total_matches == 10
    assert result.truncated is True


async def test_find_all_sort_by_date_desc(db_session):
    """sort_by='date_desc' returns docs newest-first."""
    # Seed 3 docs with explicit created_at timestamps
    docs_in_order = []
    for i, days_ago in enumerate([30, 5, 60]):
        doc = Document(
            title=f"Doc{i}",
            status="active",
            sha256=f"sha_dt_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="off-balance-sheet", language="en"))
        docs_in_order.append((doc.doc_id, days_ago))
    await db_session.flush()

    result = await find_all(db_session, "off-balance-sheet", max_results=10, sort_by="date_desc")

    # Expected order: 5 days, 30 days, 60 days
    got_order = [h.doc_id for h in result.hits]
    expected_order = [d[0] for d in sorted(docs_in_order, key=lambda x: x[1])]
    assert got_order == [str(d) for d in expected_order]


async def test_find_all_sort_by_date_asc(db_session):
    """sort_by='date_asc' returns docs oldest-first."""
    docs_in_order = []
    for i, days_ago in enumerate([30, 5, 60]):
        doc = Document(
            title=f"Doc{i}",
            status="active",
            sha256=f"sha_da_{i:020d}".encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text="off-balance-sheet", language="en"))
        docs_in_order.append((doc.doc_id, days_ago))
    await db_session.flush()

    result = await find_all(db_session, "off-balance-sheet", max_results=10, sort_by="date_asc")

    # Expected order: 60 days, 30 days, 5 days
    got_order = [h.doc_id for h in result.hits]
    expected_order = [d[0] for d in sorted(docs_in_order, key=lambda x: -x[1])]
    assert got_order == [str(d) for d in expected_order]


async def test_find_all_sort_by_invalid_raises(db_session):
    """Invalid sort_by raises ValueError."""
    with pytest.raises(ValueError, match="sort_by"):
        await find_all(db_session, "query", sort_by="title")


async def test_find_all_offset_paginates_stably(db_session):
    """Calling find_all twice with consecutive offsets yields the full set
    in stable order with no overlaps."""
    await _seed_docs_with_chunks(db_session, n_docs=12, chunks_per_doc=1)

    page1 = await find_all(db_session, "off-balance-sheet", max_results=5, offset=0)
    page2 = await find_all(db_session, "off-balance-sheet", max_results=5, offset=5)

    assert len(page1.hits) == 5
    assert len(page2.hits) == 5
    overlap = {h.doc_id for h in page1.hits} & {h.doc_id for h in page2.hits}
    assert overlap == set(), f"unexpected overlap: {overlap}"
    assert page1.total_matches == page2.total_matches == 12
    assert page1.truncated is True
    assert page2.truncated is True  # 10 < 12


async def test_find_all_presentation_brief_omits_chunk_text(db_session):
    """presentation='brief' (default) leaves top_chunk_text None."""
    await _seed_docs_with_chunks(db_session, n_docs=2, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=10, presentation="brief")

    for h in result.hits:
        assert h.top_chunk_text is None
        assert h.top_chunk_id is None
        assert h.top_chunk_page is None


async def test_find_all_presentation_full_includes_chunk_text(db_session):
    """presentation='full' populates the top_chunk_* fields."""
    await _seed_docs_with_chunks(db_session, n_docs=2, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=10, presentation="full")

    for h in result.hits:
        assert h.top_chunk_text is not None
        assert h.top_chunk_id is not None


async def test_find_all_presentation_full_clamps_max_results(db_session):
    """presentation='full' clamps max_results to 30 (token economy).

    The clamp is applied server-side and total_matches still reflects
    the true count; only `returned` (len(hits)) is bounded.
    """
    await _seed_docs_with_chunks(db_session, n_docs=50, chunks_per_doc=1)

    # Request 100 with presentation=full → server clamps to 30
    result = await find_all(db_session, "off-balance-sheet", max_results=100, presentation="full")

    assert len(result.hits) == 30
    assert result.total_matches == 50
    assert result.truncated is True


async def test_find_all_presentation_invalid_raises(db_session):
    """Invalid presentation raises ValueError."""
    from harbor_clerk.search import find_all

    with pytest.raises(ValueError, match="presentation"):
        await find_all(db_session, "query", presentation="summary")


async def test_find_all_total_matches_exceeds_reranker_pool_size(db_session):
    """REGRESSION: find_all() must enumerate beyond settings.reranker_pool_size.

    Bug: hybrid_search caps results at reranker_pool_size (default 50) when
    reranker is enabled. find_all relied on hybrid_search and inherited the cap,
    so total_matches was silently truncated to 50 even on corpora with 100+
    matches. This broke the enumeration contract.

    Fix: find_all bypasses the reranker (it operates on chunks; enumeration
    operates on docs and doesn't need the cross-encoder precision boost).
    """
    # Seed 60 distinct docs all matching "off-balance-sheet"
    await _seed_docs_with_chunks(db_session, n_docs=60, chunks_per_doc=1)

    result = await find_all(db_session, "off-balance-sheet", max_results=100)

    # Critical assertion: total_matches must reflect the full corpus, not the
    # reranker pool size. If reranker_pool_size default is 50 and find_all
    # bleeds through hybrid_search's cap, this assert will fail with
    # total_matches == 50 (or less).
    assert result.total_matches == 60, (
        f"find_all enumeration capped at {result.total_matches} (expected 60); "
        f"hybrid_search's reranker pool is leaking through"
    )
    assert len(result.hits) == 60  # all returned since max_results=100
    assert result.truncated is False


# ---------------------------------------------------------------------------
# presentation="brief" snippets (#820)
# ---------------------------------------------------------------------------

_FILLER = "The supplier shall deliver the goods described in the schedule on the agreed dates. "


def _unwrap(snippet: str) -> str:
    """The snippet's text without its trim ellipses."""
    return snippet.removeprefix("…").removesuffix("…")


async def _seed_doc(db_session, title: str, chunk_texts: list[str]):
    doc = Document(
        title=title,
        status="active",
        sha256=f"sha_snip_{title}".ljust(32, "0").encode(),
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
    )
    db_session.add(doc)
    await db_session.flush()
    for j, chunk_text in enumerate(chunk_texts):
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=j, chunk_text=chunk_text, language="en"))
    await db_session.flush()
    return doc


async def test_find_all_brief_snippet_for_every_document_bounded(db_session):
    """Every brief hit carries a snippet of its own chunk, within the bound, ellipsis-trimmed."""
    from harbor_clerk.search import _FIND_ALL_SNIPPET_MAX_CHARS

    chunks = {}
    for i in range(6):
        text = f"Document {i} opens with the off-balance-sheet entity disclosure.\n\n" + _FILLER * 12
        chunks[str((await _seed_doc(db_session, f"long{i}", [text])).doc_id)] = text
    assert all(len(t) > 4 * _FIND_ALL_SNIPPET_MAX_CHARS for t in chunks.values())

    result = await find_all(db_session, "off-balance-sheet", max_results=10)

    assert {h.doc_id for h in result.hits} == set(chunks)
    for h in result.hits:
        assert h.snippet, f"brief hit for {h.doc_title} has no snippet"
        assert len(h.snippet) <= _FIND_ALL_SNIPPET_MAX_CHARS, len(h.snippet)
        assert h.snippet.endswith("…")  # trimmed
        assert "\n" not in h.snippet
        collapsed = " ".join(chunks[h.doc_id].split())
        assert collapsed.startswith(_unwrap(h.snippet)), "no text_contains: the snippet leads the chunk"
        # brief still carries no whole chunk
        assert h.top_chunk_text is None and h.top_chunk_id is None


async def test_find_all_brief_snippet_short_chunk_is_whole(db_session):
    """A chunk within the bound comes back whole, with no ellipsis."""
    await _seed_doc(db_session, "short", ["The off-balance-sheet entity\nwas disclosed."])

    result = await find_all(db_session, "off-balance-sheet", max_results=10)

    assert [h.snippet for h in result.hits] == ["The off-balance-sheet entity was disclosed."]


async def test_find_all_brief_snippet_centred_on_text_contains(db_session):
    """With text_contains, the snippet is centred on its first occurrence, matched case-insensitively."""
    from harbor_clerk.search import _FIND_ALL_SNIPPET_MAX_CHARS

    literal = "governed by the laws of Delaware"
    text = _FILLER * 8 + f"This Agreement shall be {literal} in all respects. " + _FILLER * 8
    await _seed_doc(db_session, "centred", [text])

    result = await find_all(db_session, "agreement supplier", max_results=10, text_contains="GOVERNED BY THE LAWS")

    (hit,) = result.hits
    assert len(hit.snippet) <= _FIND_ALL_SNIPPET_MAX_CHARS
    assert hit.snippet.startswith("…") and hit.snippet.endswith("…")
    body = _unwrap(hit.snippet)
    assert literal in body
    collapsed = " ".join(text.split())
    assert body in collapsed
    at = body.index("governed by the laws")
    left, right = at, len(body) - (at + len("governed by the laws"))
    # Centred: the context either side differs by no more than a word or two of snapping.
    assert abs(left - right) <= 30, (left, right, hit.snippet)
    _assert_on_word_boundaries(collapsed, body)


def _assert_on_word_boundaries(collapsed: str, body: str) -> None:
    """The snippet body starts and ends on whole words of the collapsed chunk."""
    i = collapsed.index(body)
    j = i + len(body)
    assert i == 0 or collapsed[i - 1] == " ", f"starts mid-word: {collapsed[max(0, i - 10) : i + 10]!r}"
    assert j == len(collapsed) or collapsed[j] == " ", f"ends mid-word: {collapsed[j - 10 : j + 10]!r}"


async def test_find_all_brief_snippet_comes_from_best_chunk(db_session):
    """The snippet is cut from the chunk that earned the document's score, not another chunk."""
    from harbor_clerk.search import hybrid_search

    dense = "DENSE " + "indemnification indemnification indemnification clause. " * 3
    sparse = "SPARSE the indemnification clause appears once among many other unrelated words " + _FILLER * 3
    # The sparse chunk comes first, so a snippet cut from the document's first chunk fails.
    doc = await _seed_doc(db_session, "twochunks", [sparse, dense])

    inner = await hybrid_search(db_session, "indemnification", k=10, bypass_reranker=True)
    scored = {h.chunk_text: h.score for h in inner.hits if h.doc_id == str(doc.doc_id)}
    assert set(scored) == {dense, sparse}, "both chunks must be candidates for the test to mean anything"
    top_text = max(scored, key=scored.get)
    assert top_text == dense, "the dense chunk must outscore the first one for the test to mean anything"

    result = await find_all(db_session, "indemnification", max_results=10)

    (hit,) = result.hits
    assert " ".join(top_text.split()).startswith(_unwrap(hit.snippet))
    assert hit.score == max(scored.values())


async def test_find_all_full_keeps_whole_chunk_and_no_snippet(db_session):
    """presentation='full' is unchanged: the whole best chunk, no snippet, 30-document clamp."""
    text = "The off-balance-sheet entity disclosure.\n\n" + _FILLER * 12
    await _seed_doc(db_session, "fullwhole", [text])

    result = await find_all(db_session, "off-balance-sheet", max_results=10, presentation="full")

    (hit,) = result.hits
    assert hit.top_chunk_text == text
    assert hit.snippet is None


# --- _brief_snippet unit cases ---


def test_brief_snippet_leading_text_ends_on_a_sentence():
    from harbor_clerk.search import _brief_snippet

    text = "First sentence is here. " + "Second sentence is longer than the first one by a fair margin. " * 6
    s = _brief_snippet(text, None, max_chars=120)
    assert len(s) <= 120
    assert s.endswith(".…")
    assert text.startswith(s.removesuffix("…"))


def test_brief_snippet_falls_back_to_the_start_when_the_literal_is_absent():
    from harbor_clerk.search import _brief_snippet

    text = "alpha beta gamma delta " * 30
    assert _brief_snippet(text, "zeta", max_chars=80) == _brief_snippet(text, None, max_chars=80)


def test_brief_snippet_match_near_the_end_keeps_the_end():
    from harbor_clerk.search import _brief_snippet

    text = "alpha beta gamma delta " * 30 + "the needle phrase"
    s = _brief_snippet(text, "needle phrase", max_chars=80)
    assert len(s) <= 80
    assert s.startswith("…") and not s.endswith("…")
    assert s.endswith("the needle phrase")


def test_brief_snippet_whitespace_in_the_literal_matches_any_run():
    from harbor_clerk.search import _brief_snippet

    text = "alpha beta gamma delta " * 30 + "the needle\n\n  phrase " + "omega " * 40
    # Whitespace runs on both sides: in the chunk and in the literal.
    s = _brief_snippet(text, "NEEDLE\n\n  phrase", max_chars=80)
    assert "needle phrase" in s
    assert s.startswith("\u2026")  # centred on the match, not the leading-text fallback


def test_brief_snippet_lead_without_a_space_is_cut_at_the_bound():
    """No word boundary in the window (a long token, a URL, CJK): a hard cut, ellipsis included in the bound."""
    from harbor_clerk.search import _brief_snippet

    s = _brief_snippet("x" * 300, None, max_chars=60)
    assert len(s) == 60
    assert s == "x" * 59 + "\u2026"


def test_brief_snippet_lead_sentence_end_at_the_edge_of_the_window():
    from harbor_clerk.search import _brief_snippet

    lead = "abc defg " * 6 + "abcd"
    assert len(lead) == 58
    # A sentence ending at index max_chars - 2 is kept whole: it and the ellipsis fill the bound.
    s = _brief_snippet(lead + ". " + "more words here " * 20, None, max_chars=60)
    assert s == lead + ".\u2026"
    assert len(s) == 60
    # One character later (punctuation at max_chars - 1) it no longer fits: cut earlier, never past the bound.
    s = _brief_snippet(lead + "r. " + "more words here " * 20, None, max_chars=60)
    assert len(s) <= 60
    assert s.endswith("\u2026")


def test_brief_snippet_snaps_to_word_boundaries_wherever_the_match_falls():
    from harbor_clerk.search import _brief_snippet

    words = [f"w{i:03d}{'x' * (i % 7)}" for i in range(120)]
    text = " ".join(words)
    for k in range(10, 110, 3):
        s = _brief_snippet(text, words[k], max_chars=80)
        body = s.removeprefix("\u2026").removesuffix("\u2026")
        assert words[k] in body.split(), (k, s)
        _assert_on_word_boundaries(text, body)


def test_brief_snippet_never_exceeds_the_bound_even_when_tiny():
    from harbor_clerk.search import _brief_snippet

    texts = ["x" * 300, "alpha beta. gamma delta! " * 20, "the needle phrase " * 30]
    for max_chars in range(1, 90):
        for text in texts:
            for literal in (None, "needle", "x" * 120):
                assert len(_brief_snippet(text, literal, max_chars=max_chars)) <= max_chars, (max_chars, literal)


def test_brief_snippet_literal_longer_than_the_bound_is_cut_at_the_bound():
    from harbor_clerk.search import _brief_snippet

    literal = "word " * 40
    text = "lead in text " * 10 + literal + "tail " * 10
    s = _brief_snippet(text, literal, max_chars=60)
    assert len(s) <= 60
    assert s.startswith("…word word")


@pytest.mark.parametrize(("cap", "bound"), [(50, 50), (1000, 240), (0, None), (-5, None)])
async def test_find_all_key_cap_gives_the_chunk_prefix(db_session, cap, bound):
    """key_max_snippet_chars: the snippet is the chunk's prefix within min(cap, 240), even with
    text_contains deep in the chunk; a cap of 0 or less omits the snippet and its chunk id."""
    literal = "governed by the laws of Delaware"
    text = _FILLER * 8 + f"This Agreement shall be {literal}. " + _FILLER * 8
    assert text.index(literal) > 240
    await _seed_doc(db_session, f"capped{cap}", [text])

    result = await find_all(db_session, "agreement", max_results=10, text_contains=literal, key_max_snippet_chars=cap)

    (hit,) = result.hits
    if bound is None:
        assert hit.snippet is None and hit.snippet_chunk_id is None
        return
    assert hit.snippet_chunk_id is not None
    assert 0 < len(hit.snippet) <= bound
    assert literal not in hit.snippet
    assert " ".join(text[:bound].split()).startswith(hit.snippet.removesuffix("…"))


def test_prefix_snippet_draws_only_on_the_first_n_characters():
    """Whatever the whitespace, a capped snippet never shows text from past the chunk's first N characters."""
    from harbor_clerk.search import _prefix_snippet

    text = "alpha\n\n\n\n" * 20 + "SECRET tail words " * 20
    for n in range(1, 200):
        s = _prefix_snippet(text, n)
        assert len(s) <= n, n
        assert "SECRET" not in s or "SECRET" in text[:n], n
        assert " ".join(text[:n].split()).startswith(s.removesuffix("…")), n
    assert _prefix_snippet("short\nchunk", 50) == "short chunk"
