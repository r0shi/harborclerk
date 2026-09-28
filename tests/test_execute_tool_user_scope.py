"""Tests for execute_tool forwarding user_scope onto Principal."""

import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.scope import UserScope
from harbor_clerk.llm.tools import execute_tool


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, _engine, monkeypatch):
    """Patch async_session_factory to use the test connection so MCP tools
    see flushed/committed data from the test's db_session."""
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


@pytest.mark.asyncio
async def test_execute_tool_corpus_overview_with_no_scope_sees_all(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """No user_scope = no filter = all docs from both folders visible in overview."""
    result_str = await execute_tool(
        "corpus_overview",
        {},
        user_id=admin_user.user_id,
    )
    result = json.loads(result_str)
    # Should see all 4 docs from two_folder_corpus
    assert result.get("document_count", 0) >= 4


@pytest.mark.asyncio
async def test_execute_tool_corpus_overview_with_folder_scope_restricts(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """user_scope=folder_a → only folder_a's 2 docs visible in overview."""
    folder_a, _, docs_in_a, _ = two_folder_corpus
    scope = UserScope(folder_ids=[folder_a.folder_id])

    result_str = await execute_tool(
        "corpus_overview",
        {},
        user_id=admin_user.user_id,
        user_scope=scope,
    )
    result = json.loads(result_str)
    # With scope to folder_a only, document_count should be exactly 2
    assert result.get("document_count") == 2


@pytest.mark.asyncio
async def test_execute_tool_documents_by_date_scope_restricts_before_the_sort_and_the_limit(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """#722's review: the date tool ignored the scope every other kb_* tool applies, so a folder-scoped chat saw
    the other folder's documents. And the scope must restrict the candidates before the sort and the limit:
    folder A holds the two oldest documents, B the two newest; "latest, two of them" scoped to A is A's two, which
    a filter applied to the ten returned rows afterwards would never find."""
    from datetime import UTC, datetime, timedelta

    folder_a, _, docs_in_a, docs_in_b = two_folder_corpus
    base = datetime(2001, 1, 1, tzinfo=UTC)
    for i, d in enumerate([*docs_in_a, *docs_in_b]):
        d.created_at = base + timedelta(days=30 * i)
    await db_session.flush()
    scope = UserScope(folder_ids=[folder_a.folder_id])

    scoped = json.loads(
        await execute_tool(
            "documents_by_date", {"direction": "latest", "limit": 2}, user_id=admin_user.user_id, user_scope=scope
        )
    )
    assert [r["doc_id"] for r in scoped["results"]] == [str(d.doc_id) for d in reversed(docs_in_a)], scoped

    unscoped = json.loads(
        await execute_tool("documents_by_date", {"direction": "latest", "limit": 2}, user_id=admin_user.user_id)
    )
    assert [r["doc_id"] for r in unscoped["results"]] == [str(d.doc_id) for d in reversed(docs_in_b)], unscoped


@pytest.mark.asyncio
async def test_kb_documents_by_date_honours_a_scoped_api_key_and_an_empty_scope(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """The MCP surface: a key scoped to folder A sees A; a key scoped to a folder with no documents sees none,
    and an invalid direction is still an error there, not a silent empty page."""
    import uuid

    from harbor_clerk.api.deps import Principal
    from harbor_clerk.api.scope import KeyScope
    from harbor_clerk.mcp_server import _mcp_principal, kb_documents_by_date
    from harbor_clerk.models.watched import WatchedFolder

    folder_a, _, docs_in_a, _ = two_folder_corpus
    empty = WatchedFolder(path="/c", display_name="Folder C", auto_discovered=False)
    db_session.add(empty)
    await db_session.flush()

    def key_for(folder_id) -> Principal:
        scope = KeyScope(
            scope_topic_ids=None,
            scope_folder_ids=[str(folder_id)],
            permission_tier="search",
            tool_overrides={},
            max_snippet_chars=None,
            rate_limit_rpm=None,
            rate_limit_rph=None,
        )
        return Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)

    token = _mcp_principal.set(key_for(folder_a.folder_id))
    try:
        seen = json.loads(await kb_documents_by_date(direction="earliest", limit=50))
    finally:
        _mcp_principal.reset(token)
    assert {r["doc_id"] for r in seen["results"]} == {str(d.doc_id) for d in docs_in_a}

    token = _mcp_principal.set(key_for(empty.folder_id))
    try:
        none = json.loads(await kb_documents_by_date(direction="earliest", limit=50))
        bad = json.loads(await kb_documents_by_date(direction="sideways", limit=50))
    finally:
        _mcp_principal.reset(token)
    assert none["count"] == 0 and none["results"] == []
    assert "error" in bad and "direction" in bad["error"]


async def _doc_in_folder(db_session, folder, *, title, filename, metadata=None):
    """An active document filed under ``folder``: what the scope filter sees is the WatchedFile row."""
    import uuid

    from harbor_clerk.models.document import Document
    from harbor_clerk.models.enums import PipelineStatus
    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus

    d = Document(
        title=title,
        canonical_filename=filename,
        status="active",
        sha256=(uuid.uuid4().bytes + uuid.uuid4().bytes)[:32],
        pipeline_status=PipelineStatus.ready,
        doc_metadata=metadata or {},
    )
    db_session.add(d)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            doc_id=d.doc_id,
            relative_path=filename,
            sha256=d.sha256,
            bookmark_data=b"",
            status=WatchedFileStatus.active,
        )
    )
    await db_session.flush()
    return d


@pytest.mark.asyncio
async def test_execute_tool_verify_identifier_scope_hides_the_other_folder_on_every_match_path(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """#724: the verify tool ignored the scope every other kb_* tool applies. A chat scoped to folder A must
    get not_found for a document in folder B however the identifier would match it: title substring, filename
    substring, tika title, an identifier-like metadata key, and the word pass (#715). The same calls unscoped
    resolve, so each not_found is the scope's doing and not a fixture that never matched."""
    folder_a, folder_b, _, _ = two_folder_corpus
    by_path = {
        "title": ("Scope Title Only 724", "b-title.pdf", None),
        "filename": ("Unrelated B filename", "scope-filename-724.pdf", None),
        "tika_title": ("Unrelated B tika", "b-tika.pdf", {"tika": {"title": "Scope Tika Title 724"}}),
        "metadata_id": ("Unrelated B sidecar", "b-sidecar.pdf", {"sidecar": {"contract_id": "K-SCOPE-724"}}),
        "words": ("ArcaUsTreasuryFund_20200207_Development Agreement", "b-words.pdf", None),
    }
    identifiers = {
        "title": "Scope Title Only",
        "filename": "scope-filename-724",
        "tika_title": "scope tika title 724",
        "metadata_id": "K-SCOPE-724",
        "words": "the Arca US Treasury Fund development agreement",
    }
    in_b = {
        path: await _doc_in_folder(db_session, folder_b, title=title, filename=filename, metadata=metadata)
        for path, (title, filename, metadata) in by_path.items()
    }
    scope = UserScope(folder_ids=[folder_a.folder_id])

    for path, identifier in identifiers.items():
        unscoped = json.loads(await execute_tool("verify_identifier", {"identifier": identifier}, admin_user.user_id))
        assert unscoped["status"] == "unique" and unscoped["match"]["doc_id"] == str(in_b[path].doc_id), (
            path,
            unscoped,
        )
        scoped = json.loads(
            await execute_tool("verify_identifier", {"identifier": identifier}, admin_user.user_id, user_scope=scope)
        )
        assert scoped["status"] == "not_found", (path, scoped)
        assert in_b[path].title not in json.dumps(scoped) and str(in_b[path].doc_id) not in json.dumps(scoped), (
            path,
            scoped,
        )

    # The scope decides between unique and ambiguous too: a same-named document in B must not turn A's unique
    # match into an ambiguous pair that lists B's title.
    in_a = await _doc_in_folder(db_session, folder_a, title="Scope Title Only 724 (A copy)", filename="a-title.pdf")
    scoped = json.loads(
        await execute_tool(
            "verify_identifier", {"identifier": "Scope Title Only"}, admin_user.user_id, user_scope=scope
        )
    )
    assert scoped["status"] == "unique" and scoped["match"]["doc_id"] == str(in_a.doc_id), scoped
    assert str(in_b["title"].doc_id) not in json.dumps(scoped)
    unscoped = json.loads(
        await execute_tool("verify_identifier", {"identifier": "Scope Title Only"}, admin_user.user_id)
    )
    assert unscoped["status"] == "ambiguous" and unscoped["count"] == 2, unscoped


@pytest.mark.asyncio
async def test_kb_verify_identifier_scoped_key_applies_the_scope_before_the_metadata_cap(
    db_session, admin_user, two_folder_corpus, mock_session_factory
):
    """The MCP surface with a scoped API key. The metadata pass reads at most 100 documents, so the scope must
    restrict that read, not its result: with a hundred out-of-scope documents carrying the identifier, a key
    scoped to A still resolves A's one. A key scoped to a folder with no documents finds nothing, even for a
    title that exists."""
    import uuid

    from harbor_clerk.api.deps import Principal
    from harbor_clerk.api.scope import KeyScope
    from harbor_clerk.mcp_server import _mcp_principal, kb_verify_identifier
    from harbor_clerk.models.watched import WatchedFolder

    folder_a, folder_b, docs_in_a, _ = two_folder_corpus
    empty = WatchedFolder(path="/c", display_name="Folder C", auto_discovered=False)
    db_session.add(empty)
    await db_session.flush()
    sidecar = {"sidecar": {"contract_id": "K-CAP-724"}}
    for i in range(100):
        await _doc_in_folder(db_session, folder_b, title=f"Decoy {i}", filename=f"decoy{i}.pdf", metadata=sidecar)
    in_a = await _doc_in_folder(db_session, folder_a, title="The one in A", filename="a-cap.pdf", metadata=sidecar)

    def key_for(folder_id) -> Principal:
        scope = KeyScope(
            scope_topic_ids=None,
            scope_folder_ids=[str(folder_id)],
            permission_tier="search",
            tool_overrides={},
            max_snippet_chars=None,
            rate_limit_rpm=None,
            rate_limit_rph=None,
        )
        return Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)

    token = _mcp_principal.set(key_for(folder_a.folder_id))
    try:
        seen = json.loads(await kb_verify_identifier("K-CAP-724"))
    finally:
        _mcp_principal.reset(token)
    assert seen["status"] == "unique" and seen["match"]["doc_id"] == str(in_a.doc_id), seen

    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    try:
        everyone = json.loads(await kb_verify_identifier("K-CAP-724"))
    finally:
        _mcp_principal.reset(token)
    assert everyone["status"] == "ambiguous" and everyone.get("overflow") is True, "unscoped, the cap is reached"

    token = _mcp_principal.set(key_for(empty.folder_id))
    try:
        none = json.loads(await kb_verify_identifier(docs_in_a[0].title))
    finally:
        _mcp_principal.reset(token)
    assert none["status"] == "not_found" and docs_in_a[0].title not in json.dumps(none.get("match")), none
