"""Tests for apply_folder_scope — the pure query builder shared by
apply_key_scope and the user-side folder filter."""

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
