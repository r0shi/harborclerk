"""A deleted document's chunks are not readable by chunk_id, for any principal (#909).

Every surface that reads chunk text by id treats a chunk whose document is not
``active`` exactly as it treats an unknown chunk_id: list reads
(``kb_read_passages``, ``POST /api/passages/read``) omit it, and the single-chunk
read (``kb_expand_context``) answers "Chunk not found". The response is
identical to the unknown-id case, so a caller cannot tell the two apart.

Each surface is driven as the signed-in admin, a folder-scoped key and an
unscoped key. The deleted document keeps its active ``WatchedFile`` in the
scoped folder. Over REST a scope check alone does not hide it (``apply_key_scope``
has no status filter), so every REST case guards the new filter. Over MCP,
``_visible_doc_ids`` already filters a scoped key on status: the MCP
``scoped_key`` cases are a backstop, and the admin and unscoped-key cases are
the ones that fail without the chunk query's own filter.
"""

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.auth import create_access_token, generate_api_key, hash_api_key
from harbor_clerk.mcp_server import _mcp_principal, _resolve_principal
from harbor_clerk.models import ApiKey, Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from tests.conftest import auth_header, watch_document

# On MCP only "admin" and "unscoped_key" guard the chunk query's status filter; "scoped_key" is
# also hidden by _visible_doc_ids (see the module docstring). On REST all three guard it.
PRINCIPALS = ["admin", "scoped_key", "unscoped_key"]


@dataclass
class Corpus:
    folder_id: str
    live_chunk_ids: list[str]  # an active document's chunks, in order
    deleted_chunk_ids: list[str]  # a deleted document's chunks, in order


@pytest.fixture
async def corpus(db_session: AsyncSession, tmp_path) -> Corpus:
    from sqlalchemy import select

    from harbor_clerk.models.watched import WatchedFolder

    ids: dict[str, list[str]] = {}
    for label, status in (("live", "active"), ("gone", "deleted")):
        src = tmp_path / f"{label}.txt"
        src.write_text(label)
        doc = Document(
            title=f"{label} notes",
            status=status,
            sha256=label.encode().ljust(32, b"x"),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            source_path=str(src),
            doc_metadata={},
        )
        db_session.add(doc)
        await db_session.flush()
        await watch_document(db_session, doc, src)
        chunks = [
            Chunk(doc_id=doc.doc_id, chunk_num=i, chunk_text=f"{label} chunk {i}", language="en", ocr_used=False)
            for i in range(3)
        ]
        db_session.add_all(chunks)
        await db_session.flush()
        ids[label] = [str(c.chunk_id) for c in chunks]

    folder = (await db_session.execute(select(WatchedFolder).where(WatchedFolder.path == str(tmp_path)))).scalar_one()
    return Corpus(str(folder.folder_id), ids["live"], ids["gone"])


@pytest.fixture
async def mock_session_factory(db_session: AsyncSession, monkeypatch):
    """MCP tools open their own sessions; share the test connection with them."""
    conn = await db_session.connection()

    @asynccontextmanager
    async def _factory():
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()

    monkeypatch.setattr("harbor_clerk.mcp_server.async_session_factory", _factory)


async def _credential(db_session: AsyncSession, corpus: Corpus, admin_user, who: str) -> str:
    """A bearer credential for ``who``: the admin's JWT, or a raw full-tier key."""
    if who == "admin":
        return create_access_token(admin_user.user_id, admin_user.role.value)
    raw = generate_api_key()
    scope = [corpus.folder_id] if who == "scoped_key" else None
    db_session.add(ApiKey(name=who, key_hash=hash_api_key(raw), permission_tier="full", scope_folder_ids=scope))
    await db_session.flush()
    return raw


async def _mcp_call(db_session, corpus, admin_user, who: str, tool: str, **kwargs) -> dict:
    import harbor_clerk.mcp_server as m

    if who == "admin":
        principal = Principal(type="user", id=admin_user.user_id, role="admin")
    else:
        principal = await _resolve_principal(await _credential(db_session, corpus, admin_user, who))
        assert principal is not None
        assert (principal.key_scope.scope_folder_ids is not None) == (who == "scoped_key")
    token = _mcp_principal.set(principal)
    try:
        return json.loads(await getattr(m, tool)(**kwargs))
    finally:
        _mcp_principal.reset(token)


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_mcp_read_passages_omits_a_deleted_documents_chunk(
    db_session, corpus, mock_session_factory, admin_user, who
):
    live, gone = corpus.live_chunk_ids[1], corpus.deleted_chunk_ids[1]
    payload = await _mcp_call(
        db_session, corpus, admin_user, who, "kb_read_passages", chunk_ids=[gone, live], include_context=True
    )
    assert [p["chunk_id"] for p in payload["passages"]] == [live], payload
    assert "gone" not in json.dumps(payload)

    alone = await _mcp_call(db_session, corpus, admin_user, who, "kb_read_passages", chunk_ids=[gone])
    unknown = await _mcp_call(db_session, corpus, admin_user, who, "kb_read_passages", chunk_ids=[str(uuid.uuid4())])
    assert alone == unknown == {"passages": []}


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_mcp_expand_context_refuses_a_deleted_documents_chunk(
    db_session, corpus, mock_session_factory, admin_user, who
):
    live = await _mcp_call(
        db_session, corpus, admin_user, who, "kb_expand_context", chunk_id=corpus.live_chunk_ids[1], n=1
    )
    assert [c["chunk_id"] for c in live["chunks"]] == corpus.live_chunk_ids, live

    gone = await _mcp_call(
        db_session, corpus, admin_user, who, "kb_expand_context", chunk_id=corpus.deleted_chunk_ids[1], n=1
    )
    unknown = await _mcp_call(db_session, corpus, admin_user, who, "kb_expand_context", chunk_id=str(uuid.uuid4()))
    assert gone == unknown == {"error": "Chunk not found"}


@pytest.mark.parametrize("who", PRINCIPALS)
async def test_rest_passages_read_omits_a_deleted_documents_chunk(client, db_session, corpus, admin_user, who):
    headers = auth_header(await _credential(db_session, corpus, admin_user, who))
    live, gone = corpus.live_chunk_ids[1], corpus.deleted_chunk_ids[1]

    async def read(chunk_ids: list[str]) -> dict:
        resp = await client.post(
            "/api/passages/read", json={"chunk_ids": chunk_ids, "include_context": True}, headers=headers
        )
        assert resp.status_code == 200, resp.text[:300]
        return resp.json()

    mixed = await read([gone, live])
    assert [p["chunk_id"] for p in mixed["passages"]] == [live], mixed
    assert "gone" not in json.dumps(mixed)
    assert await read([gone]) == await read([str(uuid.uuid4())]) == {"passages": []}
