"""kb_search metadata_filter — JSONB @> containment + ? existence fallback."""

import uuid

from harbor_clerk.models import Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.search import hybrid_search


async def _seed_doc(db_session, *, title: str, metadata: dict) -> Document:
    """Helper: insert a Document with one trivial Chunk so FTS can match."""
    doc = Document(
        title=title,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata,
    )
    db_session.add(doc)
    await db_session.flush()
    chunk = Chunk(
        doc_id=doc.doc_id,
        chunk_num=0,
        chunk_text="jurisdiction indemnification arbitration clause",
        language="english",
    )
    db_session.add(chunk)
    await db_session.flush()
    return doc


async def test_metadata_filter_pins_doc_by_scalar_field(db_session):
    """A scalar filter on a scalar metadata value matches via @> containment."""
    pinnacle = await _seed_doc(
        db_session,
        title="vendor-pinnacle-A",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24}},
    )
    other = await _seed_doc(
        db_session,
        title="vendor-other",
        metadata={"sidecar": {"vendor": "Other Vendor, LLC", "term_months": 12}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={"sidecar.vendor": "Pinnacle Tech Solutions, LLC"},
    )
    cited_doc_ids = {hit.doc_id for hit in result.hits}
    assert str(pinnacle.doc_id) in cited_doc_ids
    assert str(other.doc_id) not in cited_doc_ids


async def test_metadata_filter_pins_doc_by_list_value_via_existence(db_session):
    """A scalar filter on a list-valued metadata field matches if the list
    contains the scalar (JSONB ? operator)."""
    tagged = await _seed_doc(
        db_session,
        title="tagged-alpha",
        metadata={"frontmatter": {"tags": ["alpha", "beta"]}},
    )
    untagged = await _seed_doc(
        db_session,
        title="untagged",
        metadata={"frontmatter": {"tags": ["gamma"]}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={"frontmatter.tags": "alpha"},
    )
    cited = {hit.doc_id for hit in result.hits}
    assert str(tagged.doc_id) in cited
    assert str(untagged.doc_id) not in cited


async def test_metadata_filter_combines_multiple_keys_with_and(db_session):
    """Two filter keys → AND. Both must match for a doc to be returned."""
    a = await _seed_doc(
        db_session,
        title="vendor-pinnacle-24mo",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 24}},
    )
    b = await _seed_doc(
        db_session,
        title="vendor-pinnacle-12mo",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC", "term_months": 12}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={
            "sidecar.vendor": "Pinnacle Tech Solutions, LLC",
            "sidecar.term_months": 24,
        },
    )
    cited = {hit.doc_id for hit in result.hits}
    assert str(a.doc_id) in cited
    assert str(b.doc_id) not in cited


async def test_metadata_filter_empty_match_returns_no_hits(db_session):
    """A filter that matches no docs returns an empty result set (not an error)."""
    await _seed_doc(
        db_session,
        title="vendor-pinnacle",
        metadata={"sidecar": {"vendor": "Pinnacle Tech Solutions, LLC"}},
    )
    await db_session.commit()

    result = await hybrid_search(
        db_session,
        query="jurisdiction arbitration",
        k=10,
        metadata_filter={"sidecar.vendor": "Nonexistent Vendor Co"},
    )
    assert result.hits == []


async def test_metadata_filter_absent_falls_back_to_existing_behavior(db_session):
    """When metadata_filter is None, behavior is unchanged."""
    doc = await _seed_doc(db_session, title="any", metadata={})
    await db_session.commit()
    result = await hybrid_search(db_session, query="jurisdiction arbitration", k=10, metadata_filter=None)
    assert any(hit.doc_id == str(doc.doc_id) for hit in result.hits)


async def test_metadata_filter_rejects_multi_dot_paths(db_session):
    """A multi-dot path like 'sidecar.contract.term' silently produces 'no
    match' under naive partition logic — fail loudly instead. Spec v1 is
    namespace.key only."""
    import pytest

    with pytest.raises(ValueError, match="exactly 'namespace.key'"):
        await hybrid_search(
            db_session,
            query="jurisdiction",
            k=10,
            metadata_filter={"sidecar.contract.term": 24},
        )


async def test_metadata_filter_rejects_empty_namespace_or_key(db_session):
    """Edge cases the dot-count check lets through but the partition check
    catches: leading dot (empty ns), trailing dot (empty key)."""
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        await hybrid_search(
            db_session,
            query="jurisdiction",
            k=10,
            metadata_filter={".key": "x"},
        )
    with pytest.raises(ValueError, match="non-empty"):
        await hybrid_search(
            db_session,
            query="jurisdiction",
            k=10,
            metadata_filter={"ns.": "x"},
        )


def test_apply_jsonb_metadata_filter_string_value_appends_or_clause():
    """String value: appends an OR(containment, existence) predicate."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    doc_conditions = []
    _apply_jsonb_metadata_filter({"sidecar.vendor": "Acme"}, doc_conditions)
    # One predicate appended
    assert len(doc_conditions) == 1
    # Rendered SQL contains both the @> containment and the ? existence
    # operators. compile() without literal_binds works fine because we
    # only inspect the SQL fragment, not the bound values.
    compiled = str(doc_conditions[0].compile())
    assert "@>" in compiled
    # JSONB existence operator — compiled SQL form
    assert "?" in compiled or "jsonb_exists" in compiled.lower()


def test_apply_jsonb_metadata_filter_int_value_appends_containment_only():
    """Non-string scalar: containment only, no existence OR-branch."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    doc_conditions = []
    _apply_jsonb_metadata_filter({"sidecar.term_months": 24}, doc_conditions)
    assert len(doc_conditions) == 1
    compiled = str(doc_conditions[0].compile())
    assert "@>" in compiled
    # No OR / existence — just plain containment. The compiled form for
    # OR(...) uses "OR" between the two operands; containment-only is a
    # single @> predicate with no OR.
    assert " OR " not in compiled.upper()


def test_apply_jsonb_metadata_filter_multiple_keys_append_in_order():
    """Multiple keys: each becomes its own predicate appended in input order."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    doc_conditions = []
    _apply_jsonb_metadata_filter(
        {"sidecar.vendor": "Acme", "frontmatter.author": "Alice"},
        doc_conditions,
    )
    assert len(doc_conditions) == 2


def test_apply_jsonb_metadata_filter_rejects_multi_dot_path():
    """Nested paths (two dots) raise ValueError."""
    import pytest

    from harbor_clerk.search import _apply_jsonb_metadata_filter

    with pytest.raises(ValueError, match="exactly 'namespace.key'"):
        _apply_jsonb_metadata_filter(
            {"sidecar.contract.id": "x"},
            [],
        )


def test_apply_jsonb_metadata_filter_rejects_empty_segment():
    """Empty namespace or key raises ValueError."""
    import pytest

    from harbor_clerk.search import _apply_jsonb_metadata_filter

    with pytest.raises(ValueError, match="non-empty namespace and key"):
        _apply_jsonb_metadata_filter({".vendor": "Acme"}, [])

    with pytest.raises(ValueError, match="non-empty namespace and key"):
        _apply_jsonb_metadata_filter({"sidecar.": "Acme"}, [])


def test_apply_jsonb_metadata_filter_returns_none():
    """Helper mutates doc_conditions in place; returns None (companion to
    _apply_email_metadata_filter which returns the remaining dict)."""
    from harbor_clerk.search import _apply_jsonb_metadata_filter

    result = _apply_jsonb_metadata_filter({"sidecar.vendor": "Acme"}, [])
    assert result is None
