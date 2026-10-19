"""A single ``doc_id`` under a folder scope on REST search and Find All (#873).

A folder-scoped API key, or a request ``scope``, narrows the search to its
visible documents. With a ``doc_id`` and no ``doc_ids``, ``/api/search`` used to
fill ``doc_ids`` from the visible set as well, and ``hybrid_search`` refuses
both ("Cannot specify both doc_id and doc_ids"), so the caller got a 422. The
visible ``doc_id`` alone narrows the search; one outside the scope is still an
empty 200, as before. Each case runs on both routes, through real key rows.
"""

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.auth import generate_api_key, hash_api_key
from harbor_clerk.models import ApiKey, Chunk, Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from tests.conftest import auth_header

_TEXT = "The lighthouse keeper logged the {label} storm and trimmed the lamp wick before the tide turned."

# Route, body, and the response list whose items carry ``doc_id``.
ROUTES = {
    "search": ("/api/search", {"query": "lighthouse keeper"}, "hits"),
    "find-all": ("/api/search/find-all", {"query": "lighthouse"}, "results"),
}


@dataclass
class ScopedCorpus:
    folder_a: WatchedFolder
    folder_b: WatchedFolder
    a1: Document  # in folder A
    a2: Document  # in folder A: proves the doc_id narrows within the visible set
    b1: Document  # in folder B: outside a folder-A scope


@pytest.fixture
async def corpus(db_session: AsyncSession) -> ScopedCorpus:
    folder_a = WatchedFolder(path="/scoped-a", display_name="Folder A", auto_discovered=False)
    folder_b = WatchedFolder(path="/scoped-b", display_name="Folder B", auto_discovered=False)
    db_session.add_all([folder_a, folder_b])
    await db_session.flush()

    docs: list[Document] = []
    for i, (label, folder) in enumerate([("a1", folder_a), ("a2", folder_a), ("b1", folder_b)]):
        doc = Document(
            title=f"Keeper Log {label}",
            canonical_filename=f"{label}.txt",
            status="active",
            sha256=bytes([0x70 + i] * 32),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            doc_metadata={},
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(
            WatchedFile(
                folder_id=folder.folder_id,
                doc_id=doc.doc_id,
                relative_path=f"{label}.txt",
                sha256=doc.sha256,
                bookmark_data=b"",
                status=WatchedFileStatus.active,
            )
        )
        db_session.add(
            Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=_TEXT.format(label=label), language="en", ocr_used=False)
        )
        docs.append(doc)
    await db_session.flush()
    return ScopedCorpus(folder_a, folder_b, *docs)


async def _make_key(db_session: AsyncSession, folder_ids: list[str] | None) -> str:
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            name=f"scoped-{folder_ids}",
            key_hash=hash_api_key(raw),
            permission_tier="full",
            scope_folder_ids=folder_ids,
        )
    )
    await db_session.flush()
    return raw


async def _doc_ids_returned(client, route: str, headers: dict, **extra) -> set[str]:
    path, body, items = ROUTES[route]
    resp = await client.post(path, json={**body, **extra}, headers=headers)
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    return {item["doc_id"] for item in resp.json()[items]}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_with_a_visible_doc_id_returns_that_documents_hits(client, db_session, corpus, route):
    raw = await _make_key(db_session, [str(corpus.folder_a.folder_id)])
    returned = await _doc_ids_returned(client, route, auth_header(raw), doc_id=str(corpus.a1.doc_id))
    assert returned == {str(corpus.a1.doc_id)}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_without_doc_id_still_sees_only_its_folder(client, db_session, corpus, route):
    raw = await _make_key(db_session, [str(corpus.folder_a.folder_id)])
    returned = await _doc_ids_returned(client, route, auth_header(raw))
    assert returned == {str(corpus.a1.doc_id), str(corpus.a2.doc_id)}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_with_a_doc_id_outside_its_scope_gets_nothing(client, db_session, corpus, route):
    raw = await _make_key(db_session, [str(corpus.folder_a.folder_id)])
    returned = await _doc_ids_returned(client, route, auth_header(raw), doc_id=str(corpus.b1.doc_id))
    assert returned == set()


@pytest.mark.parametrize("route", list(ROUTES))
async def test_unscoped_key_with_a_doc_id_is_unchanged(client, db_session, corpus, route):
    raw = await _make_key(db_session, None)
    returned = await _doc_ids_returned(client, route, auth_header(raw), doc_id=str(corpus.b1.doc_id))
    assert returned == {str(corpus.b1.doc_id)}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_request_folder_scope_with_a_visible_doc_id_returns_that_documents_hits(
    client, admin_token, corpus, route
):
    returned = await _doc_ids_returned(
        client,
        route,
        auth_header(admin_token),
        doc_id=str(corpus.a2.doc_id),
        scope={"folder_ids": [str(corpus.folder_a.folder_id)]},
    )
    assert returned == {str(corpus.a2.doc_id)}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_request_folder_scope_with_a_doc_id_outside_it_gets_nothing(client, admin_token, corpus, route):
    returned = await _doc_ids_returned(
        client,
        route,
        auth_header(admin_token),
        doc_id=str(corpus.b1.doc_id),
        scope={"folder_ids": [str(corpus.folder_a.folder_id)]},
    )
    assert returned == set()


@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_and_request_scope_with_a_doc_id_visible_to_both(client, db_session, corpus, route):
    """Both narrowing blocks run; the doc_id must pass each, and neither fills doc_ids."""
    raw = await _make_key(db_session, [str(corpus.folder_a.folder_id), str(corpus.folder_b.folder_id)])
    headers = auth_header(raw)
    scope = {"folder_ids": [str(corpus.folder_a.folder_id)]}
    assert await _doc_ids_returned(client, route, headers, doc_id=str(corpus.a1.doc_id), scope=scope) == {
        str(corpus.a1.doc_id)
    }
    # Visible to the key, outside the request scope.
    assert await _doc_ids_returned(client, route, headers, doc_id=str(corpus.b1.doc_id), scope=scope) == set()
