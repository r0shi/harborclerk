"""Tests for scoped API keys."""

from harbor_clerk.api.scope import (
    ADMIN_ONLY_TOOLS,
    FULL_TIER_TOOLS,
    READ_TIER_TOOLS,
    SEARCH_TIER_TOOLS,
    KeyScope,
    compute_effective_tools,
)


def test_search_tier_has_search_tools():
    tools = compute_effective_tools("search", {})
    assert "kb_search" in tools
    assert "kb_batch_search" in tools
    assert "kb_corpus_overview" in tools
    assert "kb_list_recent" in tools
    # No read tools
    assert "kb_read_passages" not in tools
    # No admin tools
    assert "kb_system_health" not in tools
    assert "kb_reprocess" not in tools


def test_read_tier_extends_search():
    tools = compute_effective_tools("read", {})
    assert "kb_search" in tools  # search tier inherited
    assert "kb_read_passages" in tools
    assert "kb_expand_context" in tools
    assert "kb_document_outline" in tools
    assert "kb_get_document" in tools
    # Not in this tier
    assert "kb_find_related" not in tools
    assert "kb_entity_search" not in tools


def test_full_tier_has_most_tools():
    tools = compute_effective_tools("full", {})
    assert "kb_read_document" in tools
    assert "kb_entity_search" in tools
    assert "kb_find_related" in tools
    assert "kb_ingest_status" in tools
    # Admin tools never included
    assert "kb_system_health" not in tools
    assert "kb_reprocess" not in tools


def test_overrides_can_remove_tool():
    tools = compute_effective_tools("full", {"kb_read_document": False})
    assert "kb_read_document" not in tools
    assert "kb_search" in tools  # other tools unchanged


def test_overrides_can_add_tool():
    tools = compute_effective_tools("search", {"kb_read_passages": True})
    assert "kb_read_passages" in tools


def test_overrides_cannot_grant_admin_tools():
    tools = compute_effective_tools("full", {"kb_system_health": True})
    assert "kb_system_health" not in tools
    tools = compute_effective_tools("full", {"kb_reprocess": True})
    assert "kb_reprocess" not in tools


def test_unknown_tier_treated_as_full():
    # Defensive — unknown tier shouldn't crash
    tools = compute_effective_tools("nonsense", {})
    assert "kb_search" in tools


def test_keyscope_default_no_restrictions():
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is True


def test_keyscope_with_topic_filter_is_restricted():
    scope = KeyScope(
        scope_topic_ids=[1, 2],
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is False


def test_admin_only_tools_constant():
    assert "kb_system_health" in ADMIN_ONLY_TOOLS
    assert "kb_reprocess" in ADMIN_ONLY_TOOLS


def test_tier_constants_are_frozen_sets():
    assert isinstance(SEARCH_TIER_TOOLS, frozenset)
    assert isinstance(READ_TIER_TOOLS, frozenset)
    assert isinstance(FULL_TIER_TOOLS, frozenset)


# --- Principal integration ---

import uuid  # noqa: E402

from harbor_clerk.api.deps import Principal  # noqa: E402


def test_principal_default_no_key_scope():
    p = Principal(type="user", id=uuid.uuid4(), role="admin")
    assert p.key_scope is None


def test_principal_with_key_scope():
    scope = KeyScope(
        scope_topic_ids=[1],
        scope_folder_ids=None,
        permission_tier="search",
        tool_overrides={},
        max_snippet_chars=500,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    assert p.key_scope is not None
    assert p.key_scope.scope_topic_ids == [1]
    assert p.key_scope.permission_tier == "search"


# --- apply_key_scope query builder ---

from sqlalchemy import select as sa_select  # noqa: E402

from harbor_clerk.api.scope import apply_key_scope  # noqa: E402
from harbor_clerk.models.document import Document  # noqa: E402


def test_apply_scope_user_principal_unchanged():
    user = Principal(type="user", id=uuid.uuid4(), role="admin")
    query = sa_select(Document)
    result = apply_key_scope(query, user)
    # Should return the same query unmodified
    assert str(result) == str(query)


def test_apply_scope_unrestricted_key_unchanged():
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    assert str(result) == str(query)


def test_apply_scope_topic_filter_adds_where():
    scope = KeyScope(
        scope_topic_ids=[1, 2],
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    sql = str(result.compile(compile_kwargs={"literal_binds": True}))
    assert "topic_id" in sql.lower()
    assert "in (1, 2)" in sql.lower()


def test_apply_scope_folder_filter_subquery():
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=["00000000-0000-0000-0000-000000000001"],
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    sql = str(result.compile(compile_kwargs={"literal_binds": True}))
    assert "watched_files" in sql.lower()
    assert "doc_id" in sql.lower()


def test_apply_scope_topic_or_folder_or_logic():
    scope = KeyScope(
        scope_topic_ids=[5],
        scope_folder_ids=["00000000-0000-0000-0000-000000000001"],
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    sql = str(result.compile(compile_kwargs={"literal_binds": True})).lower()
    # Both filters present, joined by OR
    assert "topic_id" in sql
    assert "watched_files" in sql
    assert " or " in sql


def test_apply_scope_empty_topics_is_unrestricted():
    """Empty list [] is treated the same as None — no restriction."""
    scope = KeyScope(
        scope_topic_ids=[],  # explicit empty list = no restriction
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    assert scope.is_unrestricted is True
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = sa_select(Document)
    result = apply_key_scope(query, p)
    # Should be unchanged (no WHERE clause added)
    assert str(result.compile()) == str(query.compile())


# --- Integration tests with real DB ---

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402


@pytest_asyncio.fixture
async def two_topic_docs(db_session: AsyncSession):
    """Create 2 corpus_topics + 3 documents (2 in topic A, 1 in topic B)."""
    from datetime import UTC, datetime

    from harbor_clerk.models.corpus_topic import CorpusTopic

    now = datetime.now(UTC)
    topic_a = CorpusTopic(
        topic_id=9001,
        label="ScopeTest Topic A",
        keywords=["test"],
        doc_count=2,
        representative_doc_ids=[],
        updated_at=now,
    )
    topic_b = CorpusTopic(
        topic_id=9002,
        label="ScopeTest Topic B",
        keywords=["test"],
        doc_count=1,
        representative_doc_ids=[],
        updated_at=now,
    )
    db_session.add_all([topic_a, topic_b])
    await db_session.commit()

    docs = [
        Document(title="ScopeTest A1", canonical_filename="a1.txt", topic_id=9001, status="active"),
        Document(title="ScopeTest A2", canonical_filename="a2.txt", topic_id=9001, status="active"),
        Document(title="ScopeTest B1", canonical_filename="b1.txt", topic_id=9002, status="active"),
    ]
    for d in docs:
        db_session.add(d)
    await db_session.commit()
    yield docs
    for d in docs:
        await db_session.delete(d)
    await db_session.delete(topic_a)
    await db_session.delete(topic_b)
    await db_session.commit()


@pytest.mark.asyncio
async def test_scope_filters_by_topic_in_db(db_session, two_topic_docs):
    """Real DB: key scoped to topic 9001 sees only those documents."""
    scope = KeyScope(
        scope_topic_ids=[9001],
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = apply_key_scope(sa_select(Document).where(Document.topic_id.in_([9001, 9002])), p)
    result = await db_session.execute(query)
    docs = result.scalars().all()
    assert len(docs) == 2
    assert all(d.topic_id == 9001 for d in docs)


@pytest.mark.asyncio
async def test_unrestricted_key_sees_all_in_db(db_session, two_topic_docs):
    """Real DB: unrestricted key sees all our test documents."""
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = apply_key_scope(sa_select(Document).where(Document.topic_id.in_([9001, 9002])), p)
    result = await db_session.execute(query)
    docs = result.scalars().all()
    titles = {d.title for d in docs}
    assert {"ScopeTest A1", "ScopeTest A2", "ScopeTest B1"}.issubset(titles)


@pytest.mark.asyncio
async def test_empty_topic_list_is_unrestricted_in_db(db_session, two_topic_docs):
    """Real DB: scope_topic_ids=[] is treated as unrestricted (same as None)."""
    scope = KeyScope(
        scope_topic_ids=[],
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=None,
    )
    p = Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)
    query = apply_key_scope(sa_select(Document).where(Document.topic_id.in_([9001, 9002])), p)
    result = await db_session.execute(query)
    docs = result.scalars().all()
    our_titles = {d.title for d in docs} & {"ScopeTest A1", "ScopeTest A2", "ScopeTest B1"}
    assert our_titles == {"ScopeTest A1", "ScopeTest A2", "ScopeTest B1"}
