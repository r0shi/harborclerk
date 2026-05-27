"""Tests for apply_folder_scope — the pure query builder shared by
apply_key_scope and the user-side folder filter."""

import uuid

from sqlalchemy import select

from harbor_clerk.api.scope import apply_folder_scope
from harbor_clerk.models.document import Document


async def test_apply_folder_scope_none_is_noop(db_session, two_folder_corpus):
    """folder_ids=None returns the query unchanged — all docs visible."""
    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, None)

    all_ids = {r[0] for r in (await db_session.execute(query)).all()}
    scoped_ids = {r[0] for r in (await db_session.execute(scoped)).all()}

    assert scoped_ids == all_ids


async def test_apply_folder_scope_empty_list_is_noop(db_session, two_folder_corpus):
    """folder_ids=[] also means no restriction."""
    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, [])

    all_ids = {r[0] for r in (await db_session.execute(query)).all()}
    scoped_ids = {r[0] for r in (await db_session.execute(scoped)).all()}

    assert scoped_ids == all_ids


async def test_apply_folder_scope_restricts_to_named_folder(db_session, two_folder_corpus):
    """folder_ids=[folder_a] returns only docs in folder_a."""
    folder_a, folder_b, docs_in_a, docs_in_b = two_folder_corpus

    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, [folder_a.folder_id])

    scoped_ids = {r[0] for r in (await db_session.execute(scoped)).all()}
    expected_ids = {d.doc_id for d in docs_in_a}
    assert scoped_ids == expected_ids


async def test_apply_folder_scope_excludes_removed_watched_files(db_session, two_folder_corpus, mark_one_removed):
    """A WatchedFile in `removed` status no longer surfaces its document."""
    folder_a, _, _, _ = two_folder_corpus
    removed_doc_id = await mark_one_removed(folder_a)

    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_folder_scope(query, [folder_a.folder_id])
    scoped_ids = {r[0] for r in (await db_session.execute(scoped)).all()}

    assert removed_doc_id not in scoped_ids


async def test_apply_key_scope_combined_topic_and_folder_axes(db_session, two_folder_corpus):
    """apply_key_scope with BOTH scope_topic_ids AND scope_folder_ids set
    should return docs matching either axis (OR). This path was not covered by
    existing tests before the .whereclause refactor."""
    from datetime import UTC, datetime

    from harbor_clerk.api.deps import Principal
    from harbor_clerk.api.scope import KeyScope, apply_key_scope
    from harbor_clerk.models.corpus_topic import CorpusTopic

    folder_a, folder_b, docs_in_a, docs_in_b = two_folder_corpus

    # Insert a real CorpusTopic row so the FK from Document.topic_id is satisfied.
    target_topic_id = 999
    topic = CorpusTopic(
        topic_id=target_topic_id,
        label="test-topic",
        keywords=["foo"],
        doc_count=1,
        representative_doc_ids=[],
        updated_at=datetime.now(UTC),
    )
    db_session.add(topic)
    await db_session.flush()

    # Assign the topic to one doc in folder_b so the topic axis surfaces it.
    docs_in_b[0].topic_id = target_topic_id
    await db_session.commit()

    # Build a scope with both axes: topic=999 (matches one folder_b doc) OR folder_a (all folder_a docs)
    principal = Principal(
        type="api_key",
        id=uuid.uuid4(),
        role="user",
        key_scope=KeyScope(
            scope_topic_ids=[target_topic_id],
            scope_folder_ids=[str(folder_a.folder_id)],
            permission_tier="full",
            tool_overrides={},
            max_snippet_chars=None,
            rate_limit_rpm=None,
            rate_limit_rph=None,
        ),
    )

    query = select(Document.doc_id).where(Document.status == "active")
    scoped = apply_key_scope(query, principal)
    rows = {r[0] for r in (await db_session.execute(scoped)).all()}

    # Should include: all folder_a docs + the one folder_b doc with the target topic
    expected = {d.doc_id for d in docs_in_a} | {docs_in_b[0].doc_id}
    assert rows == expected
