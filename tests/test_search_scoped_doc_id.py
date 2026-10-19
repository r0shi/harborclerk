"""A single ``doc_id`` under a scope on REST search and Find All (#873).

A scoped API key (by folder, by topic, or both), or a request ``scope``, narrows
the search to its visible documents. With a ``doc_id`` and no ``doc_ids``,
``/api/search`` used to fill ``doc_ids`` from the visible set as well, and
``hybrid_search`` refuses both ("Cannot specify both doc_id and doc_ids"), so
the caller got a 422. The visible ``doc_id`` alone narrows the search; one
outside the scope is still an empty 200, as before. Each case runs on both
routes, through real key rows. A malformed ``doc_id`` or ``doc_ids`` entry is a
422, not a 500.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.auth import generate_api_key, hash_api_key
from harbor_clerk.models import ApiKey, Chunk, Document
from harbor_clerk.models.corpus_topic import CorpusTopic
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from tests.conftest import auth_header

_TEXT = "The lighthouse keeper logged the {label} storm and trimmed the lamp wick before the tide turned."

# Route, body, and the response list whose items carry ``doc_id``.
ROUTES = {
    "search": ("/api/search", {"query": "lighthouse keeper"}, "hits"),
    "find-all": ("/api/search/find-all", {"query": "lighthouse"}, "results"),
}

# Documents: (label, folder, topic). Each document has its own topic, so a topic key
# picks documents one by one and a topic-or-folder key can reach one through each axis.
_DOCS = [("a1", "A", 1), ("a2", "A", 2), ("b1", "B", 3), ("c1", "C", 4)]


@dataclass
class ScopedCorpus:
    folders: dict[str, WatchedFolder]
    docs: dict[str, Document]

    def folder_id(self, name: str) -> str:
        return str(self.folders[name].folder_id)

    def doc_id(self, label: str) -> str:
        return str(self.docs[label].doc_id)


@dataclass(frozen=True)
class KeyScopeCase:
    """A scoped key, by folder names and topic ids, and what it sees."""

    folders: tuple[str, ...]
    topics: tuple[int, ...]
    visible: frozenset[str]
    reach_via: str  # the visible document a doc_id test searches within
    outside: str  # a document the key cannot see


# Each scope sees two or three documents and not the rest. The topic-or-folder key
# reaches b1 only through its topic, so its doc_id case exercises the topic axis.
KEY_SCOPES = {
    "folder": KeyScopeCase(("A",), (), frozenset({"a1", "a2"}), "a1", "b1"),
    "topic": KeyScopeCase((), (1, 2), frozenset({"a1", "a2"}), "a2", "b1"),
    "topic-or-folder": KeyScopeCase(("A",), (3,), frozenset({"a1", "a2", "b1"}), "b1", "c1"),
}

# Keys that see a1 and b1, for the request-scope (folder A) intersection.
KEY_SCOPES_SEEING_A1_AND_B1 = {
    "folder": (("A", "B"), ()),
    "topic": ((), (1, 3)),
    "topic-or-folder": (("A",), (3,)),
}


@pytest.fixture
async def corpus(db_session: AsyncSession) -> ScopedCorpus:
    now = datetime.now(UTC)
    folders = {
        name: WatchedFolder(path=f"/scoped-{name}", display_name=f"Folder {name}", auto_discovered=False)
        for name in ("A", "B", "C")
    }
    db_session.add_all(folders.values())
    db_session.add_all(
        CorpusTopic(
            topic_id=topic,
            label=f"Scoped Topic {topic}",
            keywords=["lighthouse"],
            doc_count=1,
            representative_doc_ids=[],
            updated_at=now,
        )
        for _, _, topic in _DOCS
    )
    await db_session.flush()

    docs: dict[str, Document] = {}
    for i, (label, folder, topic) in enumerate(_DOCS):
        doc = Document(
            title=f"Keeper Log {label}",
            canonical_filename=f"{label}.txt",
            status="active",
            sha256=bytes([0x70 + i] * 32),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            doc_metadata={},
            topic_id=topic,
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(
            WatchedFile(
                folder_id=folders[folder].folder_id,
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
        docs[label] = doc
    await db_session.flush()
    return ScopedCorpus(folders, docs)


async def _make_key(
    db_session: AsyncSession, corpus: ScopedCorpus, folders: tuple[str, ...] = (), topics: tuple[int, ...] = ()
) -> str:
    """A full-tier key scoped to ``folders`` and ``topics`` (unscoped when both are empty)."""
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            name=f"scoped-{folders}-{topics}",
            key_hash=hash_api_key(raw),
            permission_tier="full",
            scope_folder_ids=[corpus.folder_id(f) for f in folders] or None,
            scope_topic_ids=list(topics) or None,
        )
    )
    await db_session.flush()
    return raw


async def _doc_ids_returned(client, route: str, headers: dict, **extra) -> set[str]:
    path, body, items = ROUTES[route]
    resp = await client.post(path, json={**body, **extra}, headers=headers)
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    return {item["doc_id"] for item in resp.json()[items]}


@pytest.mark.parametrize("scope", list(KEY_SCOPES))
@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_with_a_visible_doc_id_returns_that_documents_hits(client, db_session, corpus, route, scope):
    case = KEY_SCOPES[scope]
    raw = await _make_key(db_session, corpus, case.folders, case.topics)
    returned = await _doc_ids_returned(client, route, auth_header(raw), doc_id=corpus.doc_id(case.reach_via))
    assert returned == {corpus.doc_id(case.reach_via)}


@pytest.mark.parametrize("scope", list(KEY_SCOPES))
@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_without_doc_id_still_sees_only_its_scope(client, db_session, corpus, route, scope):
    case = KEY_SCOPES[scope]
    raw = await _make_key(db_session, corpus, case.folders, case.topics)
    returned = await _doc_ids_returned(client, route, auth_header(raw))
    assert returned == {corpus.doc_id(label) for label in case.visible}


@pytest.mark.parametrize("scope", list(KEY_SCOPES))
@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_with_a_doc_id_outside_its_scope_gets_nothing(client, db_session, corpus, route, scope):
    case = KEY_SCOPES[scope]
    raw = await _make_key(db_session, corpus, case.folders, case.topics)
    returned = await _doc_ids_returned(client, route, auth_header(raw), doc_id=corpus.doc_id(case.outside))
    assert returned == set()


@pytest.mark.parametrize("route", list(ROUTES))
async def test_unscoped_key_with_a_doc_id_is_unchanged(client, db_session, corpus, route):
    raw = await _make_key(db_session, corpus)
    returned = await _doc_ids_returned(client, route, auth_header(raw), doc_id=corpus.doc_id("b1"))
    assert returned == {corpus.doc_id("b1")}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_request_folder_scope_with_a_visible_doc_id_returns_that_documents_hits(
    client, admin_token, corpus, route
):
    returned = await _doc_ids_returned(
        client,
        route,
        auth_header(admin_token),
        doc_id=corpus.doc_id("a2"),
        scope={"folder_ids": [corpus.folder_id("A")]},
    )
    assert returned == {corpus.doc_id("a2")}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_request_folder_scope_with_a_doc_id_outside_it_gets_nothing(client, admin_token, corpus, route):
    returned = await _doc_ids_returned(
        client,
        route,
        auth_header(admin_token),
        doc_id=corpus.doc_id("b1"),
        scope={"folder_ids": [corpus.folder_id("A")]},
    )
    assert returned == set()


@pytest.mark.parametrize("scope", list(KEY_SCOPES_SEEING_A1_AND_B1))
@pytest.mark.parametrize("route", list(ROUTES))
async def test_scoped_key_and_request_scope_with_a_doc_id_visible_to_both(client, db_session, corpus, route, scope):
    """Both narrowing blocks run; the doc_id must pass each, and neither fills doc_ids."""
    folders, topics = KEY_SCOPES_SEEING_A1_AND_B1[scope]
    headers = auth_header(await _make_key(db_session, corpus, folders, topics))
    request_scope = {"folder_ids": [corpus.folder_id("A")]}
    assert await _doc_ids_returned(client, route, headers, doc_id=corpus.doc_id("a1"), scope=request_scope) == {
        corpus.doc_id("a1")
    }
    # Visible to the key, outside the request scope.
    assert await _doc_ids_returned(client, route, headers, doc_id=corpus.doc_id("b1"), scope=request_scope) == set()


# A malformed id is refused by request validation (422), naming the field, on both routes.
# /api/search raised ValueError from uuid.UUID in the route: a 500.
MALFORMED = {
    "doc_id": ({"doc_id": "not-a-uuid"}, ("body", "doc_id")),
    "doc_ids entry": ({"doc_ids": [str(uuid.uuid4()), "not-a-uuid"]}, ("body", "doc_ids")),
}


@pytest.mark.parametrize("bad", list(MALFORMED))
@pytest.mark.parametrize("route", list(ROUTES))
async def test_a_malformed_doc_id_is_a_422(client, admin_token, route, bad):
    extra, loc = MALFORMED[bad]
    path, body, _ = ROUTES[route]
    resp = await client.post(path, json={**body, **extra}, headers=auth_header(admin_token))
    assert resp.status_code == 422, (resp.status_code, resp.text[:300])
    assert [tuple(err["loc"][:2]) for err in resp.json()["detail"]] == [loc]


@pytest.mark.parametrize("bad", list(MALFORMED))
async def test_search_names_the_malformed_doc_id(client, admin_token, bad):
    extra, _ = MALFORMED[bad]
    resp = await client.post("/api/search", json={"query": "lighthouse", **extra}, headers=auth_header(admin_token))
    assert resp.status_code == 422
    expected = "doc_id must be a UUID" if bad == "doc_id" else "doc_ids[1] must be a UUID"
    assert expected in resp.json()["detail"][0]["msg"]


async def test_search_still_accepts_every_form_uuid_parses_and_an_empty_doc_id(client, admin_token, corpus):
    """The check is the parser the route already used, so no input it accepted is refused."""
    raw = uuid.UUID(corpus.doc_id("a1")).hex
    for doc_id in (raw, raw.upper(), "{" + corpus.doc_id("a1") + "}", "urn:uuid:" + corpus.doc_id("a1")):
        assert await _doc_ids_returned(client, "search", auth_header(admin_token), doc_id=doc_id) == {
            corpus.doc_id("a1")
        }, doc_id
    # An empty doc_id is no filter, as before.
    returned = await _doc_ids_returned(client, "search", auth_header(admin_token), doc_id="")
    assert returned == {corpus.doc_id(label) for label, _, _ in _DOCS}
