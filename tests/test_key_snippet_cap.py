"""An API key's ``max_snippet_chars`` bounds every passage it receives, on every surface (#823).

One rule (``passage_cap.cap_passage_text`` / ``cap_text``): under a key with a cap
of N, a passage is the chunk's (or page's) first N characters; a cap of 0 — or a
negative value stored before create/update validated it — returns no text;
humans and keys with no cap are unaffected. Each surface below is driven with a
real ``ApiKey`` row (MCP resolves it through ``_resolve_principal``, REST through
the auth dependency), so a negative value written straight to the database takes
the same path a live key does.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.scope import KeyScope
from harbor_clerk.auth import create_access_token, generate_api_key, hash_api_key
from harbor_clerk.mcp_server import _mcp_principal, _resolve_principal
from harbor_clerk.models import ApiKey, Chunk, Document, DocumentPage
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.passage_cap import cap_passage_text, cap_text, snippet_cap
from harbor_clerk.payload_bounds import (
    HINT_MAX_CHARS,
    METADATA_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TRUNCATED_KEY,
)
from harbor_clerk.search import _brief_snippet
from tests.conftest import auth_header

ELLIPSIS = "…"
# Every response field that carries corpus passage text, on MCP and REST.
PASSAGE_FIELDS = frozenset({"text", "chunk_text", "snippet", "context_before", "context_after"})

# Single-spaced so a brief snippet's whitespace collapse is the identity and
# every passage a capped key receives is a literal prefix of its source.
_SENTENCE = "The lighthouse keeper logged the {n} storm and trimmed the lamp wick before the tide turned. "


def _long_text(label: str) -> str:
    return "".join(_SENTENCE.format(n=f"{label}-{i}") for i in range(5)).strip()  # ~470 chars


@dataclass
class Corpus:
    paged_doc: Document  # has DocumentPage rows: kb_read_document reads pages
    chunk_doc: Document  # no pages: kb_read_document falls back to chunks
    chunk_ids: list[str]  # the paged document's chunks, in order
    sources: list[str]  # every chunk and page text a passage may be cut from


@pytest.fixture
async def corpus(db_session: AsyncSession, tmp_path) -> Corpus:
    src = tmp_path / "logbook.pdf"
    src.write_bytes(b"%PDF-1.4 the whole logbook")
    paged = Document(
        title="Keeper Logbook",
        status="active",
        sha256=b"c" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
        source_path=str(src),
        doc_metadata={},
    )
    unpaged = Document(
        title="Keeper Notes",
        status="active",
        sha256=b"d" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
        doc_metadata={},
    )
    db_session.add_all([paged, unpaged])
    await db_session.flush()

    sources: list[str] = []
    chunks: list[Chunk] = []
    for i in range(3):
        text = _long_text(f"page{i}")
        sources.append(text)
        db_session.add(DocumentPage(doc_id=paged.doc_id, page_num=i + 1, page_text=text, ocr_used=False))
        chunk = Chunk(
            doc_id=paged.doc_id,
            chunk_num=i,
            chunk_text=text,
            language="en",
            ocr_used=False,
            page_start=i + 1,
            page_end=i + 1,
        )
        chunks.append(chunk)
        db_session.add(chunk)
    for i in range(2):
        text = _long_text(f"note{i}")
        sources.append(text)
        db_session.add(Chunk(doc_id=unpaged.doc_id, chunk_num=i, chunk_text=text, language="en", ocr_used=False))
    await db_session.flush()
    return Corpus(paged, unpaged, [str(c.chunk_id) for c in chunks], sources)


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


async def _make_key(db_session: AsyncSession, cap: int | None) -> str:
    """A full-tier key with ``max_snippet_chars=cap`` written straight to the row (no validation)."""
    raw = generate_api_key()
    db_session.add(ApiKey(name=f"cap-{cap}", key_hash=hash_api_key(raw), permission_tier="full", max_snippet_chars=cap))
    await db_session.flush()
    return raw


def _passages(obj: object, key: str | None = None) -> list[str | None]:
    """Every value of a passage field anywhere in a JSON payload (None and "" included)."""
    found: list[str | None] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in PASSAGE_FIELDS and (v is None or isinstance(v, str)):
                found.append(v)
            else:
                found.extend(_passages(v, k))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_passages(item, key))
    return found


def _whole_range(surface: str) -> bool:
    """A whole-document surface caps the requested range, not each page: N characters in all."""
    return "kb_read_document" in surface or "/content" in surface


def _uncapped_form(surface: str) -> Callable[[str], str]:
    """What an uncapped caller receives of a source text on ``surface``: the whole text, except where
    the surface trims by design for everyone (kb_search brief at brief_chars=400, a find-all brief row)."""
    if surface == "kb_search brief":
        return lambda s: s[:400] + ELLIPSIS if len(s) > 400 else s
    if "find_all brief" in surface or "find-all brief" in surface:
        return lambda s: _brief_snippet(s, None)
    return lambda s: s


def _assert_capped(
    payload: dict, cap: int | None, sources: list[str], *, surface: str, whole_range: bool = False
) -> None:
    assert '"doc_id"' in json.dumps(payload), f"the surface returned nothing to check: {payload}"
    values = _passages(payload)
    texts = [t for t in values if t]
    if cap is None:
        # Uncapped (a human, or a key without a cap): every passage is its source text in full,
        # or exactly the surface's own trim of it.
        expected = {_uncapped_form(surface)(src) for src in sources}
        assert texts, "an uncapped caller received no passage at all"
        for t in texts:
            assert t in expected, f"an uncapped caller received a cut passage ({len(t)} chars): {t!r}"
        return
    if cap <= 0:
        # "No text" is said one way on every surface: the field is null or absent, never "".
        assert all(v is None for v in values), f"a key capped at {cap} received passage text: {values}"
        return
    assert texts, "a capped key received no passage at all"
    for t in texts:
        body = t.removesuffix(ELLIPSIS)
        assert len(body) <= cap, f"{len(t)} chars under a cap of {cap}: {t!r}"
        assert any(s.startswith(body) for s in sources), f"not a prefix of any chunk or page: {t!r}"
    if whole_range:
        total = sum(len(t) for t in texts)
        assert total <= cap, f"{total} chars across the response under a cap of {cap}: {texts}"


# --- surfaces ---------------------------------------------------------------

Surface = Callable[[Corpus], Awaitable[dict]]


def _mcp(name: str, **kwargs) -> Surface:
    async def call(corpus: Corpus) -> dict:
        import harbor_clerk.mcp_server as m

        args = {
            k: (v(corpus) if callable(v) else v)  # a callable argument is filled from the corpus
            for k, v in kwargs.items()
        }
        return json.loads(await getattr(m, name)(**args))

    return call


MCP_SURFACES: dict[str, Surface] = {
    "kb_search full": _mcp("kb_search", query="lighthouse keeper"),
    "kb_search brief": _mcp("kb_search", query="lighthouse keeper", detail="brief", brief_chars=400),
    "kb_search faceted": _mcp("kb_search", query="lighthouse keeper", faceted=True),
    "kb_batch_search": _mcp("kb_batch_search", queries=["lighthouse keeper", "storm lamp"]),
    "kb_find_all brief": _mcp("kb_find_all", query="lighthouse"),
    "kb_find_all full": _mcp("kb_find_all", query="lighthouse", presentation="full"),
    "kb_read_passages": _mcp("kb_read_passages", chunk_ids=lambda c: c.chunk_ids[1:2], include_context=True),
    "kb_expand_context": _mcp("kb_expand_context", chunk_id=lambda c: c.chunk_ids[1], n=1),
    "kb_read_document pages": _mcp("kb_read_document", doc_id=lambda c: str(c.paged_doc.doc_id)),
    "kb_read_document chunks": _mcp("kb_read_document", doc_id=lambda c: str(c.chunk_doc.doc_id)),
}

# (method, path, JSON body); {doc_id} is the paged document, {chunk_id} its middle chunk.
REST_SURFACES: dict[str, tuple[str, str, dict | None]] = {
    "POST /api/search": ("POST", "/api/search", {"query": "lighthouse keeper"}),
    "POST /api/search faceted": ("POST", "/api/search", {"query": "lighthouse keeper", "faceted": True}),
    "POST /api/search/find-all brief": ("POST", "/api/search/find-all", {"query": "lighthouse"}),
    "POST /api/search/find-all full": (
        "POST",
        "/api/search/find-all",
        {"query": "lighthouse", "presentation": "full"},
    ),
    "POST /api/passages/read": ("POST", "/api/passages/read", {"chunk_ids": ["{chunk_id}"], "include_context": True}),
    "GET /api/docs/{doc_id}/content": ("GET", "/api/docs/{doc_id}/content", None),
    "GET /api/docs/{doc_id}/content max_chars=10000": ("GET", "/api/docs/{doc_id}/content?max_chars=10000", None),
}

CAPS = [50, 0, -5, None]


@pytest.mark.parametrize("cap", CAPS, ids=lambda c: f"cap={c}")
@pytest.mark.parametrize("surface", list(MCP_SURFACES), ids=list(MCP_SURFACES))
async def test_mcp_surface_honours_the_key_snippet_cap(db_session, corpus, mock_session_factory, surface, cap):
    principal = await _resolve_principal(await _make_key(db_session, cap))
    assert principal is not None and principal.key_scope.max_snippet_chars == cap
    token = _mcp_principal.set(principal)
    try:
        payload = await MCP_SURFACES[surface](corpus)
    finally:
        _mcp_principal.reset(token)
    _assert_capped(payload, cap, corpus.sources, surface=surface, whole_range=_whole_range(surface))


@pytest.mark.parametrize("surface", list(MCP_SURFACES), ids=list(MCP_SURFACES))
async def test_mcp_surface_is_uncapped_for_the_human(db_session, corpus, mock_session_factory, admin_user, surface):
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    try:
        payload = await MCP_SURFACES[surface](corpus)
    finally:
        _mcp_principal.reset(token)
    _assert_capped(payload, None, corpus.sources, surface=surface)


async def _rest(client, corpus: Corpus, surface: str, headers: dict) -> dict:
    method, path, body = REST_SURFACES[surface]
    path = path.replace("{doc_id}", str(corpus.paged_doc.doc_id))
    if body is not None:
        body = json.loads(json.dumps(body).replace("{chunk_id}", corpus.chunk_ids[1]))
    resp = await client.request(method, path, json=body, headers=headers)
    assert resp.status_code == 200, (resp.status_code, resp.text[:300])
    return resp.json()


@pytest.mark.parametrize("cap", CAPS, ids=lambda c: f"cap={c}")
@pytest.mark.parametrize("surface", list(REST_SURFACES), ids=list(REST_SURFACES))
async def test_rest_route_honours_the_key_snippet_cap(client, db_session, corpus, surface, cap):
    raw = await _make_key(db_session, cap)
    payload = await _rest(client, corpus, surface, auth_header(raw))
    _assert_capped(payload, cap, corpus.sources, surface=surface, whole_range=_whole_range(surface))


@pytest.mark.parametrize("surface", list(REST_SURFACES), ids=list(REST_SURFACES))
async def test_rest_route_is_uncapped_for_the_human(client, corpus, admin_user, surface):
    token = create_access_token(admin_user.user_id, admin_user.role.value)
    payload = await _rest(client, corpus, surface, auth_header(token))
    _assert_capped(payload, None, corpus.sources, surface=surface)


@pytest.mark.parametrize("cap", [50, None], ids=lambda c: f"cap={c}")
async def test_rest_content_negative_max_chars_returns_no_text(client, db_session, corpus, cap):
    """A negative max_chars was a slice bound: ``text[:-1]`` is all but the last character, and
    ``min(-1, cap)`` let a capped key read a whole page that way."""
    raw = await _make_key(db_session, cap)
    path = f"/api/docs/{corpus.paged_doc.doc_id}/content?max_chars=-1"
    resp = await client.get(path, headers=auth_header(raw))
    assert resp.status_code == 200, resp.text[:300]
    payload = resp.json()
    assert [p["text"] for p in payload["pages"]] == [""]
    assert payload["total_chars"] == 0


@pytest.mark.parametrize("cap", [50, 0, -5], ids=lambda c: f"cap={c}")
async def test_rest_download_is_refused_to_a_capped_key(client, db_session, corpus, cap):
    """The original's bytes are the whole text: a capped key never receives them, even with downloads on."""
    from harbor_clerk.config import get_settings

    get_settings().allow_source_download = True  # reset by the autouse settings fixture
    raw = await _make_key(db_session, cap)
    resp = await client.get(f"/api/docs/{corpus.paged_doc.doc_id}/download", headers=auth_header(raw))
    assert resp.status_code == 403, resp.text[:300]
    assert "snippet cap" in resp.json()["detail"]


async def test_rest_download_still_serves_an_uncapped_key(client, db_session, corpus):
    from harbor_clerk.config import get_settings

    get_settings().allow_source_download = True
    raw = await _make_key(db_session, None)
    resp = await client.get(f"/api/docs/{corpus.paged_doc.doc_id}/download", headers=auth_header(raw))
    assert resp.status_code == 200, resp.text[:300]
    assert resp.content == b"%PDF-1.4 the whole logbook"


# --- the helper -------------------------------------------------------------


def _key(cap: int | None) -> Principal:
    scope = KeyScope(
        scope_topic_ids=None,
        scope_folder_ids=None,
        permission_tier="full",
        tool_overrides={},
        max_snippet_chars=cap,
        rate_limit_rpm=None,
        rate_limit_rph=None,
    )
    return Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=scope)


@pytest.mark.parametrize(
    ("cap", "expected"),
    [(None, "abcdefghij"), (4, "abcd"), (10, "abcdefghij"), (50, "abcdefghij"), (0, None), (-5, None)],
)
def test_cap_passage_text_is_the_prefix_and_clamps_a_negative_cap(cap, expected):
    assert cap_passage_text("abcdefghij", _key(cap)) == expected
    assert snippet_cap(_key(cap)) == (None if cap is None else max(0, cap))


def test_cap_passage_text_leaves_humans_and_oauth_unchanged():
    for principal in (
        Principal(type="user", id=uuid.uuid4(), role="admin"),
        Principal(type="oauth", id=uuid.uuid4(), role="user"),
    ):
        assert snippet_cap(principal) is None
        assert cap_passage_text("abcdefghij", principal) == "abcdefghij"
    assert cap_text("abcdefghij", None) == "abcdefghij"


# --- key create / update validation ------------------------------------------


INT32_MAX = 2**31 - 1


@pytest.mark.parametrize("value", [-1, -500, INT32_MAX + 1])
async def test_create_key_rejects_a_snippet_cap_out_of_range(client, admin_token, value):
    """Negative, or past the 32-bit column (which would pass validation and then 500 on INSERT)."""
    resp = await client.post(
        "/api/api-keys", json={"name": "neg", "max_snippet_chars": value}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 422, resp.text[:300]


@pytest.mark.parametrize("field", ["rate_limit_rpm", "rate_limit_rph"])
@pytest.mark.parametrize("value", [-1, INT32_MAX + 1])
async def test_create_and_patch_reject_a_rate_limit_out_of_range(client, admin_token, field, value):
    resp = await client.post("/api/api-keys", json={"name": "rl", field: value}, headers=auth_header(admin_token))
    assert resp.status_code == 422, resp.text[:300]
    created = await client.post("/api/api-keys", json={"name": "rl-ok"}, headers=auth_header(admin_token))
    key_id = created.json()["key_id"]
    resp = await client.patch(f"/api/api-keys/{key_id}", json={field: value}, headers=auth_header(admin_token))
    assert resp.status_code == 422, resp.text[:300]


@pytest.mark.parametrize("value", [None, 0, 1, INT32_MAX])
async def test_create_key_accepts_a_rate_limit_in_range(client, admin_token, value):
    body = {"name": "rl", "rate_limit_rpm": value, "rate_limit_rph": value}
    resp = await client.post("/api/api-keys", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text[:300]


@pytest.mark.parametrize("value", [None, 0, 50, INT32_MAX])
async def test_create_key_accepts_none_zero_and_positive(client, admin_token, value):
    resp = await client.post(
        "/api/api-keys", json={"name": "ok", "max_snippet_chars": value}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 201, resp.text[:300]


async def test_patch_key_rejects_a_negative_snippet_cap_and_accepts_zero(client, admin_token):
    created = await client.post("/api/api-keys", json={"name": "patchable"}, headers=auth_header(admin_token))
    key_id = created.json()["key_id"]
    for bad in (-3, INT32_MAX + 1):
        resp = await client.patch(
            f"/api/api-keys/{key_id}", json={"max_snippet_chars": bad}, headers=auth_header(admin_token)
        )
        assert resp.status_code == 422, resp.text[:300]
    resp = await client.patch(
        f"/api/api-keys/{key_id}", json={"max_snippet_chars": 0}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["max_snippet_chars"] == 0
    assert "max 0 chars" in resp.json()["scope_summary"]
    resp = await client.patch(
        f"/api/api-keys/{key_id}", json={"max_snippet_chars": None}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 200 and resp.json()["max_snippet_chars"] is None


async def test_scope_summary_shows_a_legacy_negative_cap_as_zero(client, db_session, admin_token):
    """A cap stored negative before validation applies as 0, and the key list says so."""
    await _make_key(db_session, -5)
    resp = await client.get("/api/api-keys", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text[:300]
    (row,) = [k for k in resp.json() if k["name"] == "cap--5"]
    assert "max 0 chars" in row["scope_summary"], row["scope_summary"]


@pytest.mark.parametrize("name", ["", "   "])
async def test_create_and_patch_reject_a_blank_key_name(client, admin_token, name):
    resp = await client.post("/api/api-keys", json={"name": name}, headers=auth_header(admin_token))
    assert resp.status_code == 422, resp.text[:300]
    created = await client.post("/api/api-keys", json={"name": "named"}, headers=auth_header(admin_token))
    key_id = created.json()["key_id"]
    resp = await client.patch(f"/api/api-keys/{key_id}", json={"name": name}, headers=auth_header(admin_token))
    assert resp.status_code == 422, resp.text[:300]


async def test_key_name_is_stored_stripped(client, admin_token):
    created = await client.post("/api/api-keys", json={"name": "  Claude  "}, headers=auth_header(admin_token))
    assert created.status_code == 201 and created.json()["name"] == "Claude"
    key_id = created.json()["key_id"]
    resp = await client.patch(f"/api/api-keys/{key_id}", json={"name": " Codex "}, headers=auth_header(admin_token))
    assert resp.status_code == 200 and resp.json()["name"] == "Codex"


# --- derived text: text_contains, summaries, metadata, hints, entities (#831) --------------------
#
# The cap bounds the document's text, not only passages: a capped key's text_contains answers only
# about each chunk's first N characters, summaries and metadata strings are their first N, entity
# rows lose their offsets. Every MCP/tool payload (and every key over REST) also carries a size
# bound on summaries and metadata; the human's web app over REST does not.

# A literal inside every capped prefix this file uses (chars 15-40 of the paged doc's chunk 1) and
# one past it (its fourth sentence, ~300 chars in).
_LITERAL_IN_PREFIX = "keeper logged the page1-0"
_LITERAL_PAST_CAP = "page1-3 storm"

# MCP principals: a key at each cap, the human (a JWT, as the in-process chat and research tools
# run) and an OAuth agent. REST has no OAuth principal.
MCP_PRINCIPALS = [f"key cap={c}" for c in CAPS] + ["human", "oauth"]
REST_PRINCIPALS = [f"key cap={c}" for c in CAPS] + ["human"]


def _cap_of(who: str) -> int | None:
    if who.startswith("key cap="):
        raw = who.removeprefix("key cap=")
        return None if raw == "None" else int(raw)
    return None


async def _mcp_as(db_session, admin_user, who: str, call: Callable[[], Awaitable[str]]) -> dict:
    if who == "human":
        principal = Principal(type="user", id=admin_user.user_id, role="admin")
    elif who == "oauth":
        principal = Principal(type="oauth", id=uuid.uuid4(), role="user")
    else:
        principal = await _resolve_principal(await _make_key(db_session, _cap_of(who)))
    token = _mcp_principal.set(principal)
    try:
        return json.loads(await call())
    finally:
        _mcp_principal.reset(token)


async def _rest_headers(db_session, admin_user, who: str) -> dict:
    if who == "human":
        return auth_header(create_access_token(admin_user.user_id, admin_user.role.value))
    return auth_header(await _make_key(db_session, _cap_of(who)))


def _literal_visible(literal: str, cap: int | None, source: str) -> bool:
    return cap is None or literal in source[: max(0, cap)]


def _tc_mcp(name: str, **kwargs):
    async def call(literal: str) -> str:
        import harbor_clerk.mcp_server as m

        return await getattr(m, name)(text_contains=literal, **kwargs)

    return call


TEXT_CONTAINS_MCP: dict[str, Callable[[str], Awaitable[str]]] = {
    "kb_search": _tc_mcp("kb_search", query="lighthouse keeper"),
    "kb_find_all": _tc_mcp("kb_find_all", query="lighthouse"),
}
TEXT_CONTAINS_REST = {
    "POST /api/search": ("/api/search", {"query": "lighthouse keeper"}),
    "POST /api/search/find-all": ("/api/search/find-all", {"query": "lighthouse"}),
}


@pytest.mark.parametrize("literal", [_LITERAL_IN_PREFIX, _LITERAL_PAST_CAP])
@pytest.mark.parametrize("who", MCP_PRINCIPALS)
@pytest.mark.parametrize("surface", list(TEXT_CONTAINS_MCP))
async def test_mcp_text_contains_matches_only_within_a_capped_keys_prefix(
    db_session, corpus, mock_session_factory, admin_user, surface, who, literal
):
    source = corpus.sources[1]  # the paged document's chunk 1
    assert literal in source
    payload = await _mcp_as(db_session, admin_user, who, lambda: TEXT_CONTAINS_MCP[surface](literal))
    found = str(corpus.paged_doc.doc_id) in json.dumps(payload)
    assert found == _literal_visible(literal, _cap_of(who), source), (who, literal, payload)
    # A capped key is always told where the literal was matched, found or not, so the note reveals
    # nothing about the result; nobody else gets the note.
    cap = _cap_of(who)
    if cap is None:
        assert "text_contains_scope" not in payload, payload
    else:
        expected = f"first {cap} characters" if cap > 0 else "snippet cap is 0"
        assert expected in payload.get("text_contains_scope", ""), payload


@pytest.mark.parametrize("literal", [_LITERAL_IN_PREFIX, _LITERAL_PAST_CAP])
@pytest.mark.parametrize("who", REST_PRINCIPALS)
@pytest.mark.parametrize("surface", list(TEXT_CONTAINS_REST))
async def test_rest_text_contains_matches_only_within_a_capped_keys_prefix(
    client, db_session, corpus, admin_user, surface, who, literal
):
    source = corpus.sources[1]
    path, body = TEXT_CONTAINS_REST[surface]
    resp = await client.post(
        path, json={**body, "text_contains": literal}, headers=await _rest_headers(db_session, admin_user, who)
    )
    assert resp.status_code == 200, resp.text[:300]
    found = str(corpus.paged_doc.doc_id) in resp.text
    assert found == _literal_visible(literal, _cap_of(who), source), (who, literal, resp.text[:300])


@pytest.mark.parametrize("literal", [_LITERAL_IN_PREFIX, _LITERAL_PAST_CAP])
async def test_chat_find_all_text_contains_is_unchanged_for_the_human(
    db_session, corpus, mock_session_factory, admin_user, literal
):
    """The in-process chat tool runs as the human: no cap, so a literal anywhere in a chunk matches."""
    from harbor_clerk.llm.tools import execute_tool

    out = await execute_tool(
        "find_all_documents", {"query": "lighthouse", "text_contains": literal}, user_id=admin_user.user_id
    )
    assert str(corpus.paged_doc.doc_id) in out


async def _seed_chunk_doc(db_session: AsyncSession, title: str, text: str) -> Document:
    doc = Document(
        title=title,
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
        doc_metadata={},
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en", ocr_used=False))
    await db_session.flush()
    return doc


async def _matching(db_session, literal: str, cap: int | None, docs: list[Document]) -> set[str]:
    from harbor_clerk.search import hybrid_search

    result = await hybrid_search(
        db_session,
        "clause",
        k=10,
        doc_ids=[d.doc_id for d in docs],
        text_contains=literal,
        key_snippet_cap=cap,
    )
    return {h.doc_title for h in result.hits}


async def test_text_contains_under_a_cap_answers_only_about_the_prefix(db_session):
    """Two chunks share their first N characters and differ right after: under a cap of N every
    probe, word boundaries included, answers the same for both, so nothing past the cap leaks."""
    prefix = "The lease clause names the keep"
    cap = len(prefix)
    a = await _seed_chunk_doc(db_session, "A", prefix + "er of the clause records.")
    b = await _seed_chunk_doc(db_session, "B", prefix + " clause and the keeper.")
    probes = ["keep", "keep ", " keep ", "keeper", "keep clause", "the keep", "clause names", "records"]
    for probe in probes:
        got = await _matching(db_session, probe, cap, [a, b])
        assert got in (set(), {"A", "B"}), (probe, got)
    assert await _matching(db_session, "keeper", None, [a, b]) == {"A", "B"}  # uncapped: anywhere
    assert await _matching(db_session, "keep clause", None, [a, b]) == {"B"}
    assert await _matching(db_session, "clause names", cap, [a, b]) == {"A", "B"}


async def test_text_contains_prefix_is_the_raw_prefix_folded(db_session):
    """The prefix is the chunk's first N raw characters, then folded: folding first would bring
    text from past the cap into it (forty line breaks fold to one space)."""
    doc = await _seed_chunk_doc(db_session, "W", "clause word" + "\n" * 40 + "secret tail")
    assert await _matching(db_session, "word secret", None, [doc]) == {"W"}
    assert await _matching(db_session, "word secret", 20, [doc]) == set()
    assert await _matching(db_session, "clause word", 20, [doc]) == {"W"}
    assert await _matching(db_session, "clause", 0, [doc]) == set()


# --- summaries, metadata, hints ------------------------------------------------------------------

SECRET = "SECRET"  # sits past the 50-character cap in every derived string, before the 200 hint bound


def _derived_metadata(tag: str) -> dict:
    notes = {
        f"note_{i:02d}": f"{tag} note {i:02d} of the keeper's log, kept by hand at night. {SECRET}-NOTE "
        + "storm log " * 80
        for i in range(20)
    }
    return {
        "tika": {
            "title": f"Ledger {tag}",
            "description": f"{tag} description of the keeper's ledger entries, written by hand. {SECRET}-DESC-{tag} "
            + "entry " * 300,
            "pages": 3,
        },
        "sidecar": {
            "vendor": f"{tag} Vendor of lamp oil, wicks and brass fittings, est. 1902. {SECRET}-VEND-{tag}",
            "tags": [f"{tag} tag naming the lamp room and the tide tables, both. {SECRET}-TAG", "short"],
            **notes,
        },
    }


def _derived_summary(tag: str) -> str:
    return f"{tag} summary of the keeper's ledger: storms, lamps and wicks. {SECRET}-SUM-{tag} " + "more words " * 250


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


@dataclass
class Derived:
    a: Document
    b: Document
    summaries: list[str]
    meta_strings: list[str]


@pytest.fixture
async def derived(db_session: AsyncSession) -> Derived:
    """Two documents with identical chunk text (so a search ties them and the discriminator hint
    fires), long summaries, a large metadata payload with differing values, embeddings (so each is
    the other's related document) and an entity mention 300 characters into the chunk."""
    from harbor_clerk.models import Entity

    text = "The lighthouse keeper ledger records every storm, lamp and wick. " * 8
    emb = [0.0] * 768
    emb[0] = 1.0
    docs = []
    for tag in ("A", "B"):
        doc = Document(
            title=f"Keeper Ledger {tag}",
            status="active",
            sha256=(tag * 32).encode(),
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            summary=_derived_summary(tag),
            doc_metadata=_derived_metadata(tag),
        )
        db_session.add(doc)
        await db_session.flush()
        chunk = Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en", ocr_used=False, embedding=emb)
        db_session.add(chunk)
        await db_session.flush()
        db_session.add(
            Entity(
                chunk_id=chunk.chunk_id,
                doc_id=doc.doc_id,
                entity_text="Keeper Ledger Company",
                entity_type="ORG",
                start_char=300,
                end_char=321,
            )
        )
        docs.append(doc)
    await db_session.flush()
    for tag in ("A", "B"):
        assert 50 < _derived_summary(tag).index(SECRET) < 150
        for s in _strings(_derived_metadata(tag)):
            assert SECRET not in s or 50 < s.index(SECRET) < 150, s
    return Derived(
        docs[0],
        docs[1],
        [_derived_summary(t) for t in ("A", "B")],
        [s for t in ("A", "B") for s in _strings(_derived_metadata(t))],
    )


# Every payload field holding metadata values, with its whole-payload bound.
_METADATA_FIELDS = {
    "metadata": METADATA_MAX_CHARS,
    "differing_metadata": METADATA_MAX_CHARS,
    "discriminating_fields": HINT_MAX_CHARS,
}


def _derived_values(obj: object) -> tuple[list[str | None], list[str | None], list[tuple[str, dict]]]:
    """(summaries, metadata strings, (field, dict) for every metadata-bearing dict) in a payload."""
    summaries: list[str | None] = []
    strings: list[str | None] = []
    blocks: list[tuple[str, dict]] = []

    def leaves(v: object) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k != TRUNCATED_KEY:
                    leaves(x)
        elif isinstance(v, list):
            for x in v:
                leaves(x)
        elif v is None or isinstance(v, str):
            strings.append(v)

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "summary" and (v is None or isinstance(v, str)):
                    summaries.append(v)
                elif k in _METADATA_FIELDS and isinstance(v, dict):
                    blocks.append((k, v))
                    leaves(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    return summaries, strings, blocks


def _assert_derived(payload: dict, d: Derived, cap: int | None, *, bounded: bool, surface: str) -> None:
    summaries, strings, blocks = _derived_values(payload)
    dump = json.dumps(payload, ensure_ascii=False)
    assert summaries or strings, f"{surface} returned no summary or metadata to check: {dump[:300]}"
    if cap is not None and cap <= 0:
        assert all(s is None for s in summaries), summaries
        assert all(s is None for s in strings), strings
        assert SECRET not in dump
        return
    if cap is not None:
        assert SECRET not in dump, f"text past a cap of {cap} reached the key"
        for s in summaries:
            assert s is not None and len(s) <= cap and any(src.startswith(s) for src in d.summaries), s
        for s in strings:
            assert s is not None and len(s) <= cap and any(src.startswith(s) for src in d.meta_strings), s
        return
    assert SECRET in dump, "an uncapped caller saw none of the derived text"
    if not bounded:
        for s in summaries:
            assert s in d.summaries, f"the human's web app got a cut summary ({len(s or '')} chars)"
        return
    for s in summaries:
        assert s is not None and len(s) <= SUMMARY_MAX_CHARS, len(s or "")
        assert any(src.startswith(s.removesuffix(ELLIPSIS)) for src in d.summaries), s
        assert s.endswith(ELLIPSIS), "every source summary is past the bound"
    for s in strings:
        assert s is not None and len(s) <= METADATA_VALUE_MAX_CHARS, len(s or "")
        assert any(src.startswith(s.removesuffix(ELLIPSIS)) for src in d.meta_strings), s
    for field, block in blocks:
        size = len(json.dumps(block, ensure_ascii=False, separators=(",", ":")))
        assert size <= _METADATA_FIELDS[field], (field, size)
    for field, block in blocks:
        if field == "metadata":
            assert block.get(TRUNCATED_KEY), "a payload past the bound must say what it cut"


def _d_mcp(name: str, **kwargs):
    async def call(d: Derived) -> str:
        import harbor_clerk.mcp_server as m

        return await getattr(m, name)(**{k: (v(d) if callable(v) else v) for k, v in kwargs.items()})

    return call


DERIVED_MCP: dict[str, Callable[[Derived], Awaitable[str]]] = {
    "kb_get_document": _d_mcp("kb_get_document", doc_id=lambda d: str(d.a.doc_id)),
    "kb_list_recent": _d_mcp("kb_list_recent"),
    "kb_corpus_overview": _d_mcp("kb_corpus_overview"),
    "kb_find_related": _d_mcp("kb_find_related", doc_id=lambda d: str(d.a.doc_id)),
    "kb_search discriminator_hint": _d_mcp("kb_search", query="lighthouse keeper ledger"),
    "kb_batch_search discriminator_hint": _d_mcp("kb_batch_search", queries=["lighthouse keeper ledger"]),
    "kb_verify_identifier": _d_mcp("kb_verify_identifier", identifier="Keeper Ledger"),
}
DERIVED_REST = ["/api/docs", "/api/docs/overview", "/api/docs/{a}", "/api/docs/{a}/related"]


@pytest.mark.parametrize("who", MCP_PRINCIPALS)
@pytest.mark.parametrize("surface", list(DERIVED_MCP))
async def test_mcp_derived_text_is_capped_for_keys_and_bounded_for_everyone(
    db_session, derived, mock_session_factory, admin_user, surface, who
):
    payload = await _mcp_as(db_session, admin_user, who, lambda: DERIVED_MCP[surface](derived))
    cap = _cap_of(who)
    if "discriminator_hint" in surface:
        if cap is not None and cap <= 0:
            # At a cap of 0 every string compares as null, and the two documents differ in no
            # number: there is nothing the key could tell apart, so no hint (#831).
            assert "discriminator_hint" not in json.dumps(payload), json.dumps(payload)[:300]
            assert SECRET not in json.dumps(payload)
            return
        assert "discriminator_hint" in json.dumps(payload), f"the hint did not fire: {json.dumps(payload)[:300]}"
        if cap is None:  # three fields from two short ones and twenty-one past the hint bound: one is cut
            assert all(h["differing_metadata"].get(TRUNCATED_KEY) for h in _hints(payload))
        for hint in _hints(payload):
            for entry in hint["differing_metadata"].get(TRUNCATED_KEY, []):
                # Field and document by id: a title may contain dots, or be shared.
                assert entry.keys() <= {"field", "doc_id", "at"} or entry.keys() == {"more"}, entry
                if "doc_id" in entry:
                    assert entry["doc_id"] in (str(derived.a.doc_id), str(derived.b.doc_id)), entry
    if surface == "kb_verify_identifier":
        assert payload.get("status") == "ambiguous", payload
        if cap is not None and cap <= 0:
            # Every string compares as null at a cap of 0, so no field tells the candidates apart.
            assert all(c["discriminating_fields"] == {} for c in payload["candidates"]), payload["candidates"]
            assert "differ on" not in payload["suggestion"] and SECRET not in json.dumps(payload)
            return
    _assert_derived(payload, derived, _cap_of(who), bounded=True, surface=surface)


@pytest.mark.parametrize("who", REST_PRINCIPALS)
@pytest.mark.parametrize("path", DERIVED_REST)
async def test_rest_derived_text_is_capped_and_bounded_for_keys_and_whole_for_the_human(
    client, db_session, derived, admin_user, path, who
):
    resp = await client.get(
        path.replace("{a}", str(derived.a.doc_id)), headers=await _rest_headers(db_session, admin_user, who)
    )
    assert resp.status_code == 200, resp.text[:300]
    _assert_derived(resp.json(), derived, _cap_of(who), bounded=who != "human", surface=path)


async def test_chat_get_document_is_bounded_in_process(db_session, derived, mock_session_factory, admin_user):
    """The chat tools run as the human, and their payload goes into a local model's context: the
    size bound applies there (a 64 KB sidecar must not land whole), the cap does not."""
    from harbor_clerk.llm.tools import execute_tool

    out = await execute_tool("get_document", {"doc_id": str(derived.a.doc_id)}, user_id=admin_user.user_id)
    _assert_derived(json.loads(out), derived, None, bounded=True, surface="chat get_document")


@pytest.mark.parametrize("who", MCP_PRINCIPALS)
async def test_mcp_entity_search_drops_offsets_for_a_capped_key(
    db_session, derived, mock_session_factory, admin_user, who
):
    import harbor_clerk.mcp_server as m

    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_entity_search("Keeper Ledger"))
    rows = payload["entities"]
    assert rows and all(r["entity_text"] == "Keeper Ledger Company" for r in rows)
    if _cap_of(who) is None:
        assert all(r["start_char"] == 300 and r["end_char"] == 321 for r in rows)
    else:
        assert all("start_char" not in r and "end_char" not in r for r in rows), rows


@pytest.mark.parametrize("who", REST_PRINCIPALS)
@pytest.mark.parametrize("path", ["/api/docs/{a}/entities", "/api/docs/entities/autocomplete?q=Keeper"])
async def test_rest_entity_routes_never_carry_offsets(client, db_session, derived, admin_user, path, who):
    resp = await client.get(
        path.replace("{a}", str(derived.a.doc_id)), headers=await _rest_headers(db_session, admin_user, who)
    )
    assert resp.status_code == 200, resp.text[:300]
    assert "Keeper Ledger Company" in resp.text
    assert "start_char" not in resp.text and "end_char" not in resp.text


def _hints(payload: dict) -> list[dict]:
    """Every discriminator_hint in a kb_search or kb_batch_search payload."""
    if "discriminator_hint" in payload:
        return [payload["discriminator_hint"]]
    return [r["discriminator_hint"] for r in payload.get("results", []) if "discriminator_hint" in r]


# --- the discriminator suggestion names only whole values (#831 review) --------------------------


async def _seed_hint_pair(
    db_session: AsyncSession, meta: Callable[[str], dict], tags: tuple[str, ...] = ("A", "B")
) -> list[Document]:
    """Documents (one per tag) with the same chunk text (a search ties them) and metadata ``meta(tag)``."""
    text = "The harbour pilot ledger records every tide, buoy and beacon. " * 6
    docs = []
    for tag in tags:
        doc = Document(
            title=f"Pilot Ledger {tag}",
            status="active",
            sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            doc_metadata=meta(tag),
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en", ocr_used=False))
        docs.append(doc)
    await db_session.flush()
    return docs


async def _hint_as(db_session, admin_user, who: str, k: int = 10) -> dict | None:
    import harbor_clerk.mcp_server as m

    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_search(query="harbour pilot ledger", k=k))
    assert "error" not in payload, payload
    return payload.get("discriminator_hint")


def _filter_values(suggestion: str) -> list[str]:
    """The value reprs a suggestion offers as metadata_filter values."""
    import re

    offered = re.findall(r"metadata_filter=\{'[^']+': (.+?)\}", suggestion)
    listed = re.findall(
        r"=('(?:[^'\\]|\\.)*'|-?\d+(?:\.\d+)?|True|False|None)", suggestion.split("Available values:")[-1]
    )
    return offered + listed if "metadata_filter=" in suggestion else []


_LONG_VENDOR = "Acme Holdings International Shipping and Chandlery Company " + "x" * 300


@pytest.mark.parametrize("who", ["human", "oauth", "key cap=None"])
async def test_suggestion_never_quotes_a_value_cut_by_the_hint_bound(db_session, mock_session_factory, admin_user, who):
    """A value past HINT_VALUE_MAX_CHARS is shown cut; metadata_filter matches the whole stored value,
    so quoting the cut one would name a filter that finds nothing."""
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": f"{_LONG_VENDOR} {t}"}})
    hint = await _hint_as(db_session, admin_user, who)
    assert hint is not None
    assert ELLIPSIS in json.dumps(hint["differing_metadata"], ensure_ascii=False)  # shown cut
    assert "metadata_filter=" not in hint["suggestion"], hint["suggestion"]
    assert ELLIPSIS not in hint["suggestion"] and "Acme" not in hint["suggestion"]
    assert "doc_id" in hint["suggestion"]


@pytest.mark.parametrize("who", ["human", "key cap=None", "key cap=50"])
async def test_suggestion_quotes_a_whole_value(db_session, mock_session_factory, admin_user, who):
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"region": f"North Basin {t}"}})
    hint = await _hint_as(db_session, admin_user, who)
    assert hint is not None
    values = _filter_values(hint["suggestion"])
    assert values, hint["suggestion"]
    assert set(values) <= {"'North Basin A'", "'North Basin B'"}, values


async def test_suggestion_under_a_cap_skips_a_value_longer_than_the_cap(db_session, mock_session_factory, admin_user):
    """At cap=50 a 62-character vendor is shown as its first 50 characters: never offered as a filter;
    the short region, shown whole, is. Three documents with three vendors and two regions make the
    vendor rank first (more distinct values), so without the cap check it is always the one offered,
    whatever the set iteration order."""
    await _seed_hint_pair(
        db_session,
        lambda t: {"sidecar": {"vendor": f"{t} " + "v" * 60, "region": "North Basin " + ("A" if t == "A" else "B")}},
        tags=("A", "B", "C"),
    )
    hint = await _hint_as(db_session, admin_user, "key cap=50")
    assert hint is not None
    assert "sidecar.vendor" in hint["differing_metadata"]
    values = _filter_values(hint["suggestion"])
    assert values and set(values) <= {"'North Basin A'", "'North Basin B'"}, hint["suggestion"]
    assert "v" * 20 not in hint["suggestion"]


@pytest.mark.parametrize("cap", [0, -5])
async def test_suggestion_at_cap_zero_offers_no_null_string(db_session, mock_session_factory, admin_user, cap):
    """At a cap of 0 strings compare as null: only the number differs, and it is offered whole."""
    await _seed_hint_pair(
        db_session,
        lambda t: {"sidecar": {"vendor": f"Vendor {t}", "term_months": 12 if t == "A" else 24}},
    )
    hint = await _hint_as(db_session, admin_user, f"key cap={cap}")
    assert hint is not None
    assert list(k for k in hint["differing_metadata"] if k != TRUNCATED_KEY) == ["sidecar.term_months"]
    assert "None" not in hint["suggestion"] and "Vendor" not in json.dumps(hint)
    assert set(_filter_values(hint["suggestion"])) <= {"12", "24"} and _filter_values(hint["suggestion"])


async def test_suggestion_at_cap_zero_with_only_strings_has_no_hint(db_session, mock_session_factory, admin_user):
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": f"Vendor {t}"}})
    assert await _hint_as(db_session, admin_user, "key cap=0") is None
    assert await _hint_as(db_session, admin_user, "key cap=None") is not None


async def test_values_equal_within_the_cap_do_not_differ_for_the_key(db_session, mock_session_factory, admin_user):
    """Two vendors that share their first 50 characters are one value to a cap=50 key: reporting the
    field as differing would tell it the values differ past its cap."""
    shared = "S" * 50
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": shared + ("-alpha" if t == "A" else "-beta")}})
    assert await _hint_as(db_session, admin_user, "key cap=50") is None
    uncapped = await _hint_as(db_session, admin_user, "key cap=None")
    assert uncapped is not None and "sidecar.vendor" in uncapped["differing_metadata"]
    assert await _hint_as(db_session, admin_user, "key cap=60") is not None  # past the shared prefix: visible


# --- metadata_filter and kb_verify_identifier under a cap (#831 review) ----------------------------

_LONG_FILTER = "Acme Holdings International Shipping Company Limited"  # 52 characters
assert len(_LONG_FILTER) > 50


def _f_mcp(name: str, **kwargs):
    async def call(metadata_filter: dict) -> str:
        import harbor_clerk.mcp_server as m

        return await getattr(m, name)(metadata_filter=metadata_filter, **kwargs)

    return call


FILTER_MCP: dict[str, Callable[[dict], Awaitable[str]]] = {
    "kb_search": _f_mcp("kb_search", query="lighthouse keeper"),
    "kb_find_all": _f_mcp("kb_find_all", query="lighthouse"),
    "kb_batch_search": _f_mcp("kb_batch_search", queries=["lighthouse keeper"]),
    "kb_documents_by_date": _f_mcp("kb_documents_by_date", direction="earliest"),
}
FILTER_REST = {
    "POST /api/search": ("/api/search", {"query": "lighthouse keeper"}),
    "POST /api/search/find-all": ("/api/search/find-all", {"query": "lighthouse"}),
}
# (filter, refused at cap=50, refused at cap=0)
FILTER_CASES = {
    "long string": ({"sidecar.vendor": _LONG_FILTER}, True, True),
    "short string": ({"sidecar.vendor": "Acme"}, False, True),
    # Even "" tests metadata text (that the stored value is empty); a cap-0 key sees none.
    "empty string": ({"sidecar.vendor": ""}, False, True),
    # At cap 0 every string shows as null, so a null filter would tell a real null from hidden text.
    "null": ({"sidecar.vendor": None}, False, True),
    "list with a long string": ({"sidecar.tags": ["pilotage", _LONG_FILTER]}, True, True),
    "number": ({"sidecar.term_months": 24}, False, False),
}


def _refused_for(who: str, case: str) -> bool:
    cap = _cap_of(who)
    _, at_50, at_0 = FILTER_CASES[case]
    if cap is None:
        return False
    return at_0 if cap <= 0 else at_50


@pytest.mark.parametrize("case", list(FILTER_CASES))
@pytest.mark.parametrize("who", MCP_PRINCIPALS)
@pytest.mark.parametrize("surface", list(FILTER_MCP))
async def test_mcp_metadata_filter_past_the_cap_is_refused(
    db_session, corpus, mock_session_factory, admin_user, surface, who, case
):
    metadata_filter = FILTER_CASES[case][0]
    payload = await _mcp_as(db_session, admin_user, who, lambda: FILTER_MCP[surface](metadata_filter))
    if _refused_for(who, case):
        assert "snippet cap" in payload.get("error", ""), payload
    else:
        assert "error" not in payload, payload


@pytest.mark.parametrize("case", list(FILTER_CASES))
@pytest.mark.parametrize("who", REST_PRINCIPALS)
@pytest.mark.parametrize("surface", list(FILTER_REST))
async def test_rest_metadata_filter_past_the_cap_is_refused(client, db_session, corpus, admin_user, surface, who, case):
    path, body = FILTER_REST[surface]
    resp = await client.post(
        path,
        json={**body, "metadata_filter": FILTER_CASES[case][0]},
        headers=await _rest_headers(db_session, admin_user, who),
    )
    if _refused_for(who, case):
        assert resp.status_code == 422, resp.text[:300]
        assert "snippet cap" in resp.json()["detail"]
    else:
        assert resp.status_code == 200, resp.text[:300]


_LONG_ID = "HC-CONTRACT-2024-NORTH-BASIN-PILOTAGE-AGREEMENT-0001"  # 52 characters
assert len(_LONG_ID) > 50


@pytest.mark.parametrize(
    ("who", "identifier", "found"),
    [
        ("key cap=None", _LONG_ID, True),
        ("human", _LONG_ID, True),
        ("oauth", _LONG_ID, True),
        ("key cap=50", _LONG_ID, False),  # longer than the cap: a value the key never sees whole
        ("key cap=50", "PO-7731", True),  # within the cap, and so is the stored value
        ("key cap=0", "PO-7731", False),
        ("key cap=-5", "PO-7731", False),
        # A short identifier equal (whitespace folded) to a stored value whose first 50 characters
        # are blanks: the key sees none of it, so the match must not run.
        ("key cap=None", "PO-9", True),
        ("key cap=50", "PO-9", False),
    ],
)
async def test_verify_identifier_matches_metadata_leaves_only_within_the_cap(
    db_session, mock_session_factory, admin_user, who, identifier, found
):
    import harbor_clerk.mcp_server as m

    for tag, ident in (("one", _LONG_ID), ("two", "PO-7731"), ("three", " " * 60 + "PO-9")):
        db_session.add(
            Document(
                title=f"Unrelated filing {tag}",
                status="active",
                sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
                pipeline_status=PipelineStatus.ready,
                mime_type="text/plain",
                doc_metadata={"sidecar": {"contract_id": ident}},
            )
        )
    await db_session.flush()
    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_verify_identifier(identifier))
    assert (payload["status"] == "unique") is found, payload
    if not found:
        # The identifier exists (in metadata the key may not check): the tool must not claim absence.
        assert payload["status"] == "not_found", payload
        _assert_capped_not_found(payload)


def _assert_capped_not_found(payload: dict) -> None:
    assert "does not exist in the corpus" not in payload["instruction"], payload["instruction"]
    assert "not found, not that it does not exist" in payload["instruction"], payload["instruction"]


@pytest.mark.parametrize("who", MCP_PRINCIPALS)
async def test_verify_identifier_not_found_claims_absence_only_when_everything_was_checked(
    db_session, mock_session_factory, admin_user, who
):
    """An identifier nobody holds. Uncapped callers checked every field, so "it does not exist" is true
    and they get the plain instruction; every capped key gets the softened one, whether or not anything
    was left unchecked, so the wording reveals nothing."""
    import harbor_clerk.mcp_server as m

    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_verify_identifier("ZZ-NO-SUCH-IDENT-4242"))
    assert payload["status"] == "not_found", payload
    if _cap_of(who) is None:
        assert "it does not exist in the corpus" in payload["instruction"], payload["instruction"]
    else:
        _assert_capped_not_found(payload)


async def test_verify_identifier_title_matching_is_unchanged_under_a_cap(db_session, mock_session_factory, admin_user):
    """Titles are outside the cap: a long title still resolves for a cap=50 key."""
    import harbor_clerk.mcp_server as m

    title = "Master Pilotage Services Agreement between North Basin and Harbour Board"
    db_session.add(
        Document(
            title=title,
            status="active",
            sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            doc_metadata={},
        )
    )
    await db_session.flush()
    payload = await _mcp_as(db_session, admin_user, "key cap=50", lambda: m.kb_verify_identifier(title))
    assert payload["status"] == "unique", payload


# --- second review: verify_identifier comparisons, tika.title, email filters, dates (#831) -------


@pytest.mark.parametrize(("who", "differs"), [("key cap=None", True), ("key cap=50", False), ("key cap=0", False)])
async def test_verify_identifier_compares_values_as_the_key_sees_them(
    db_session, mock_session_factory, admin_user, who, differs
):
    """Two vendors that differ only past the 50th character: a cap=50 key sees one value (a cap-0 key
    sees nulls), so it must not be told the candidates differ on the vendor."""
    import harbor_clerk.mcp_server as m

    shared = "S" * 50
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": shared + ("-alpha" if t == "A" else "-beta")}})
    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_verify_identifier("Pilot Ledger"))
    assert payload["status"] == "ambiguous", payload
    fields = [set(c["discriminating_fields"]) - {TRUNCATED_KEY} for c in payload["candidates"]]
    assert all(("sidecar.vendor" in f) is differs for f in fields), payload["candidates"]
    assert ("sidecar.vendor" in payload["suggestion"]) is differs, payload["suggestion"]
    assert "alpha" not in json.dumps(payload) or differs


_LONG_TIKA_TITLE = "Pilotage Agreement for the North Basin, executed in duplicate"  # 61 characters
assert len(_LONG_TIKA_TITLE) > 50


@pytest.mark.parametrize(
    ("who", "identifier", "found"),
    [
        ("key cap=None", _LONG_TIKA_TITLE, True),
        ("human", _LONG_TIKA_TITLE, True),
        ("key cap=50", _LONG_TIKA_TITLE, False),  # a dc:title the key never sees whole
        ("key cap=50", "Harbour Tariff 2024", True),  # within the cap
        ("key cap=0", "Harbour Tariff 2024", False),
    ],
)
async def test_verify_identifier_tika_title_matches_only_within_the_cap(
    db_session, mock_session_factory, admin_user, who, identifier, found
):
    """tika.title is a metadata string (bound_metadata caps it) and Document.title is not set from it:
    under a cap the exact tika.title match runs only on a stored value within the cap."""
    import harbor_clerk.mcp_server as m

    for tag, tika_title in (("one", _LONG_TIKA_TITLE), ("two", "Harbour Tariff 2024")):
        db_session.add(
            Document(
                title=f"scan_{tag}.pdf",
                status="active",
                sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
                pipeline_status=PipelineStatus.ready,
                mime_type="application/pdf",
                doc_metadata={"tika": {"title": tika_title}},
            )
        )
    await db_session.flush()
    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_verify_identifier(identifier))
    assert (payload["status"] == "unique") is found, payload
    if not found:
        assert payload["status"] == "not_found", payload
        _assert_capped_not_found(payload)


_MESSAGE_ID = "<20240115.0001.pilotage-notice@mail.northbasin.example>"

# Every email.* filter key, with values past a 50-character cap where the field allows it: mail headers
# are envelope fields outside the snippet cap (owner's decision on #837), so none is refused.
MAIL_FILTERS = {
    "from_address": {"email.from_address": "harbourmaster.north-basin.pilotage@port-authority.example.org"},
    "from_name": {"email.from_name": "Harbour Master of the North Basin Pilotage Station, Port Authority"},
    "from_name_contains": {"email.from_name_contains": "Harbour Master"},
    "subject": {"email.subject": "Pilotage notice for the North Basin, January sailings and tides"},
    "subject_contains": {"email.subject_contains": "Pilotage"},
    "to_addresses": {"email.to_addresses": ["pilots.on.duty.north-basin@port-authority.example.org", "x@y.z"]},
    "cc_addresses": {"email.cc_addresses": "operations.desk.north-basin@port-authority.example.org"},
    "message_id": {"email.message_id": _MESSAGE_ID},
    "thread_id": {"email.thread_id": "thread-2024-01-north-basin-pilotage-notices-january"},
}
assert len(MAIL_FILTERS) == 9


@pytest.mark.parametrize("name", list(MAIL_FILTERS))
@pytest.mark.parametrize("who", ["key cap=50", "key cap=0"])
@pytest.mark.parametrize("surface", list(FILTER_MCP))
async def test_mcp_mail_header_filters_pass_for_a_capped_key(
    db_session, corpus, mock_session_factory, admin_user, surface, who, name
):
    """Mail headers are envelope fields, like a title: a capped key, even at 0, may filter on any."""
    payload = await _mcp_as(db_session, admin_user, who, lambda: FILTER_MCP[surface](MAIL_FILTERS[name]))
    assert "error" not in payload, payload


@pytest.mark.parametrize("name", list(MAIL_FILTERS))
@pytest.mark.parametrize("who", ["key cap=50", "key cap=0"])
@pytest.mark.parametrize("surface", list(FILTER_REST))
async def test_rest_mail_header_filters_pass_for_a_capped_key(
    client, db_session, corpus, admin_user, surface, who, name
):
    path, body = FILTER_REST[surface]
    resp = await client.post(
        path,
        json={**body, "metadata_filter": MAIL_FILTERS[name]},
        headers=await _rest_headers(db_session, admin_user, who),
    )
    assert resp.status_code == 200, resp.text[:300]


def test_a_citation_bounds_a_sender_address_at_the_rfc_limit():
    """The one key-facing place a mail address appears: the citation's display sender, when the
    message has no display name. RFC 5321 caps an address at 254 characters; so does the citation."""
    from harbor_clerk.source_ref import format_email_citation

    citation = format_email_citation(from_address="a" * 400 + "@example.org", subject="Notice")
    sender = citation.removeprefix("Email from ").split(", ")[0]
    assert len(sender) == 254 and sender.endswith(ELLIPSIS), len(sender)
    assert format_email_citation(from_address="hm@nb.example") == "Email from hm@nb.example"


@pytest.mark.parametrize("cap", [0, -5, 10])
async def test_a_capped_key_can_send_the_date_filter_the_hint_suggests(
    db_session, mock_session_factory, admin_user, cap
):
    """ISO dates on the date fields are structured data on both sides: the hint offers one whole as a
    filter, and the same key's search with exactly that filter is accepted and finds the document."""
    import ast
    import re

    import harbor_clerk.mcp_server as m

    dates = {"A": "2024-01-15T08:00:00Z", "B": "2024-02-01T09:30:00Z"}
    # Only the date differs, so the suggestion has one field to offer (no tie for set order to break).
    docs = await _seed_hint_pair(db_session, lambda t: {"tika": {"created_at": dates[t]}})
    hint = await _hint_as(db_session, admin_user, f"key cap={cap}")
    assert hint is not None
    match = re.search(r"metadata_filter=(\{.*?\})", hint["suggestion"])
    assert match, hint["suggestion"]
    suggested = ast.literal_eval(match.group(1))
    assert list(suggested) == ["tika.created_at"] and suggested["tika.created_at"] in dates.values(), suggested
    payload = await _mcp_as(
        db_session,
        admin_user,
        f"key cap={cap}",
        lambda: m.kb_search(query="harbour pilot ledger", metadata_filter=suggested),
    )
    assert "error" not in payload, payload
    wanted = next(
        str(d.doc_id) for d, t in zip(docs, ("A", "B"), strict=True) if dates[t] == suggested["tika.created_at"]
    )
    assert {h["doc_id"] for h in payload["hits"]} == {wanted}, payload["hits"]


@pytest.mark.parametrize(
    ("cap", "value"),
    [(10, "sometime in the spring before the storm"), (0, "spring"), (5, "2024-13-45")],
)
async def test_a_non_iso_string_on_a_date_field_is_refused_past_the_cap(
    db_session, corpus, mock_session_factory, admin_user, cap, value
):
    """Only a value that parses as an ISO date passes on a date field; anything else is text."""
    payload = await _mcp_as(
        db_session, admin_user, f"key cap={cap}", lambda: FILTER_MCP["kb_search"]({"frontmatter.date": value})
    )
    assert "snippet cap" in payload.get("error", ""), payload


@pytest.mark.parametrize("cap", [50, 10, 0])
async def test_iso_dates_in_the_date_fields_are_structured_data(db_session, mock_session_factory, admin_user, cap):
    """kb_documents_by_date sorts, filters and reports on these dates whole; kb_get_document does not cut
    an ISO date there either. Prose stored under a date key is text, and is capped."""
    import harbor_clerk.mcp_server as m

    doc = Document(
        title="Tide table",
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
        doc_metadata={
            "tika": {"created_at": "2024-01-15T08:00:00Z", "title": "Tide table for the North Basin pilots"},
            "frontmatter": {"date": "sometime in the spring before the great storm of that year"},
            "sidecar": {"date": "2024-02-01"},
        },
    )
    db_session.add(doc)
    await db_session.flush()
    payload = await _mcp_as(db_session, admin_user, f"key cap={cap}", lambda: m.kb_get_document(doc_id=str(doc.doc_id)))
    meta = payload["metadata"]
    assert meta["tika"]["created_at"] == "2024-01-15T08:00:00Z"
    assert meta["sidecar"]["date"] == "2024-02-01"
    prose = "sometime in the spring before the great storm of that year"
    assert meta["frontmatter"]["date"] == (prose[:cap] if cap > 0 else None)
    assert meta["tika"]["title"] == ("Tide table for the North Basin pilots"[:cap] if cap > 0 else None)


@pytest.mark.parametrize("cap", [5, 0])
async def test_iso_dates_in_a_list_on_a_date_field_display_whole_as_the_filter_accepts_them(
    db_session, mock_session_factory, admin_user, cap
):
    """Dates are structured wherever they sit on a date path: a list of ISO dates displays whole, so the
    filter that accepts each element confirms nothing the key was not shown."""
    import harbor_clerk.mcp_server as m

    dates = ["2024-01-15", "2025-03-02"]
    docs = await _seed_hint_pair(
        db_session, lambda t: {"frontmatter": {"date": dates if t == "A" else ["2023-07-01"], "note": "logbook"}}
    )
    payload = await _mcp_as(
        db_session, admin_user, f"key cap={cap}", lambda: m.kb_get_document(doc_id=str(docs[0].doc_id))
    )
    assert payload["metadata"]["frontmatter"]["date"] == dates, payload["metadata"]
    assert payload["metadata"]["frontmatter"]["note"] == ("logbook"[:cap] if cap > 0 else None)
    found = await _mcp_as(
        db_session,
        admin_user,
        f"key cap={cap}",
        lambda: m.kb_search(query="harbour pilot ledger", metadata_filter={"frontmatter.date": "2025-03-02"}),
    )
    assert "error" not in found, found
    assert {h["doc_id"] for h in found["hits"]} == {str(docs[0].doc_id)}, found["hits"]


# --- fifth review: nulls in the hint and in filters, dict children, strict dates (#831) ----------


@pytest.mark.parametrize("who", ["human", "key cap=None", "key cap=50"])
async def test_a_null_candidate_does_not_block_the_suggestion(db_session, mock_session_factory, admin_user, who):
    """vendor "Acme" against a JSON null: the whole value is offered, the null is neither offered nor a
    reason to offer nothing."""
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": "Acme" if t == "A" else None}})
    hint = await _hint_as(db_session, admin_user, who)
    assert hint is not None
    assert "metadata_filter={'sidecar.vendor': 'Acme'}" in hint["suggestion"], hint["suggestion"]
    assert "None" not in hint["suggestion"].split("Available values:")[-1], hint["suggestion"]


@pytest.mark.parametrize(
    ("meta", "reason"),
    [
        (lambda t: {"sidecar": {"party": {"name": f"Party {t}"}}}, "sidecar.party (not filterable)"),
        (lambda t: {"sidecar": {"tags": [{"k": t}]}}, "sidecar.tags (not filterable)"),
        (lambda t: {"sidecar": {"vendor": f"{_LONG_VENDOR} {t}"}}, "sidecar.vendor (cut)"),
        (lambda t: {"sidecar": {"tags": [f"{_LONG_VENDOR} {t}"]}}, "sidecar.tags (cut)"),
    ],
    ids=["dict", "list-of-dicts", "cut", "list-element-cut"],
)
async def test_no_filter_suggestion_states_the_reason(db_session, mock_session_factory, admin_user, meta, reason):
    await _seed_hint_pair(db_session, meta)
    hint = await _hint_as(db_session, admin_user, "human")
    assert hint is not None
    assert reason in hint["suggestion"], hint["suggestion"]
    assert "metadata_filter={" not in hint["suggestion"]


@pytest.mark.parametrize("cap", [5, 0])
async def test_a_date_in_a_dict_child_is_text(db_session, corpus, mock_session_factory, admin_user, cap):
    """Only a scalar or a list element directly on a date field is structured; a dict child under one
    is text: displayed cut, refused as a filter past the cap."""
    import harbor_clerk.mcp_server as m

    doc = Document(
        title="Nested date",
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
        doc_metadata={"tika": {"created_at": {"k": "2024-01-15"}}},
    )
    db_session.add(doc)
    await db_session.flush()
    who = f"key cap={cap}"
    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_get_document(doc_id=str(doc.doc_id)))
    assert payload["metadata"]["tika"]["created_at"] == {"k": "2024-01-15"[:cap] if cap > 0 else None}
    refused = await _mcp_as(
        db_session, admin_user, who, lambda: FILTER_MCP["kb_search"]({"tika.created_at": {"k": "2024-01-15"}})
    )
    assert "snippet cap" in refused.get("error", ""), refused


# Each edge string, and whether it is a strict ISO date (Python and Postgres must agree).
_DATE_EDGES = {
    "2024-01-15": True,
    "2024-01-15T08:00": True,
    "2024-01-15T08:00:00": True,
    "2024-01-15 08:00:00": True,
    "2024-01-15T08:00:00Z": True,
    "2024-01-15T08:00:00.123456": True,
    "2024-01-15T08:00:00+05:30": True,
    "2024-01-15T08:00:00-15:59": True,
    "2024-01-15T08:00:00.1234567": False,
    "2024-01-15T08:00:00+16:00": False,
    "2024-01-15T24:00:00": False,
    "2024-01-15T08:00:60": False,
    "2024-01-15T8:00": False,
    "2024-01-15T08": False,
    "2024-01-15t08:00": False,
    "2024-01-15 08:00:00 PST": False,
    "2024-01-15 and more": False,
    " 2024-01-15": False,
    "2024-01-15\n": False,
    "2024-02-30": False,
    "2024-13-45": False,
    "0000-01-01": False,
    "20240115": False,
    "2024-W03-1": False,
    "N/A": False,
}


async def test_the_strict_date_rule_agrees_in_python_and_postgres(db_session):
    """The display and filter exemption (Python) and the capped date tool (SQL) accept the same values."""
    from sqlalchemy import literal, select

    from harbor_clerk.mcp_lookup_tools import _strict_timestamp_cast
    from harbor_clerk.passage_cap import is_structured_date

    for value, expected in _DATE_EDGES.items():
        python = is_structured_date("tika.created_at", value)
        sql = (await db_session.execute(select(_strict_timestamp_cast(literal(value)).isnot(None)))).scalar()
        assert python is expected, (value, python)
        assert sql is expected, (value, sql)


@pytest.mark.parametrize(("who", "source"), [("key cap=10", "ingest"), ("key cap=None", "tika.created_at")])
async def test_the_capped_date_tool_uses_only_strict_dates(db_session, mock_session_factory, admin_user, who, source):
    """'2024-01-15 08:00:00 PST' shows as '2024-01-15' at cap 10, so the capped date tool must not range
    over the time the key cannot see; uncapped callers keep the wider reading."""
    import harbor_clerk.mcp_server as m

    doc = Document(
        title="Tide notice",
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
        doc_metadata={"tika": {"created_at": "2024-01-15 08:00:00 PST"}},
    )
    db_session.add(doc)
    await db_session.flush()
    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_documents_by_date(direction="earliest"))
    (row,) = [r for r in payload["results"] if r["doc_id"] == str(doc.doc_id)]
    assert row["date_source"] == source, row
    if source == "tika.created_at":
        assert row["date"].startswith("2024-01-15T16:00:00"), row


# --- sixth review: the bound in the chat's own unit (#831) ---------------------------------------

_WORST_TEXT = {
    "cjk": "港の灯台守は嵐の夜に灯を守り、潮の満ち引きを日誌に記した。",
    "french": "Le gardien du phare a consigné la tempête, l'été où la marée dépassa les écueils. ",
}


@pytest.mark.parametrize("script", list(_WORST_TEXT))
async def test_kb_get_document_fits_the_tight_fit_chat_budget_end_to_end(
    db_session, mock_session_factory, admin_user, script
):
    """A worst-case document (a summary and metadata far past every bound, all non-ASCII) through the
    in-process chat tool: the string it returns fits one tool result at the tight-fit context, so
    _truncate_for_llm leaves it unchanged and it is still valid JSON. Each non-ASCII character is a
    6-character escape on the wire; a bound counted in code points let this reach 28,000 characters."""
    from harbor_clerk.llm.chat import _tool_result_max_chars, _truncate_for_llm
    from harbor_clerk.llm.models import TIGHT_FIT_CONTEXT
    from harbor_clerk.llm.tools import execute_tool
    from harbor_clerk.payload_bounds import CHAT_RESULT_BUDGET_FLOOR_CHARS

    budget = _tool_result_max_chars(TIGHT_FIT_CONTEXT)
    assert budget == CHAT_RESULT_BUDGET_FLOOR_CHARS
    text = _WORST_TEXT[script]
    long = text * 200
    metadata = {
        "tika": {"title": text * 3, "description": long, "subject": long},
        "frontmatter": {f"champ_{i:02d}": long for i in range(15)},
        "sidecar": {"notes": long * 10, "tags": [text * 4 for _ in range(30)], "count": 7},
    }
    doc = Document(
        title=text[:40],
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
        summary=long,
        doc_metadata=metadata,
    )
    db_session.add(doc)
    await db_session.flush()
    out = await execute_tool("get_document", {"doc_id": str(doc.doc_id)}, user_id=admin_user.user_id)
    assert len(out) <= budget, f"{len(out)} characters returned against a budget of {budget}"
    assert _truncate_for_llm(out, budget) == out
    payload = json.loads(out)
    assert payload["summary"].endswith(ELLIPSIS) and payload["metadata"][TRUNCATED_KEY]


# --- seventh review: "whole" is what differing_metadata shows; the suggestion is bounded (#831) -----


@pytest.mark.parametrize(
    "value",
    [
        "港の灯台守の日誌" * 7,  # 56 code points, 336 escaped characters: shown cut at the 200 hint bound
        '"\n' * 60,  # 120 ASCII characters that escape to 240
    ],
    ids=["cjk", "escapes"],
)
async def test_a_value_shown_cut_in_the_hint_is_not_offered(db_session, mock_session_factory, admin_user, value):
    """The hint bound counts escaped characters: a value under 200 code points can still show cut in
    differing_metadata, and the suggestion must then not quote the stored value as if it were whole."""
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": f"{t}{value}"}})
    hint = await _hint_as(db_session, admin_user, "human")
    assert hint is not None
    shown = hint["differing_metadata"]["sidecar.vendor"]
    assert all(v.endswith(ELLIPSIS) for v in shown.values()), shown
    assert "metadata_filter={" not in hint["suggestion"], hint["suggestion"]
    assert "sidecar.vendor (cut)" in hint["suggestion"], hint["suggestion"]


async def test_an_entry_dropped_by_the_hint_bound_is_not_offered(db_session, mock_session_factory, admin_user):
    """Twenty-five candidates with 190-character values overflow the hint's 4,000-character bound: some
    (field, document) entries are dropped, so the field cannot be offered."""
    tags = tuple(f"{i:02d}" for i in range(25))
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": f"Vendor {t} " + "v" * 180}}, tags=tags)
    hint = await _hint_as(db_session, admin_user, "human", k=25)
    assert hint is not None
    shown = hint["differing_metadata"].get("sidecar.vendor", {})
    assert len(shown) < 25, "the bound did not drop any entry"
    assert hint["differing_metadata"][TRUNCATED_KEY]
    assert "metadata_filter={" not in hint["suggestion"], hint["suggestion"]
    assert "dropped by the size bound" in hint["suggestion"], hint["suggestion"]


async def test_the_suggestion_stays_within_its_bound(db_session, mock_session_factory, admin_user):
    """Ten candidates with whole CJK values (180 escaped characters each): "Available values" lists as
    many as fit within SUGGESTION_MAX_CHARS and says how many it left out."""
    from harbor_clerk.payload_bounds import SUGGESTION_MAX_CHARS, escaped_len

    tags = tuple(f"{i:02d}" for i in range(10))
    await _seed_hint_pair(db_session, lambda t: {"sidecar": {"vendor": t + "港の灯台守" * 5 + "日誌"}}, tags=tags)
    hint = await _hint_as(db_session, admin_user, "human")
    assert hint is not None
    suggestion = hint["suggestion"]
    assert "metadata_filter={'sidecar.vendor':" in suggestion, suggestion
    assert escaped_len(suggestion) <= SUGGESTION_MAX_CHARS, escaped_len(suggestion)
    assert "more not listed" in suggestion, suggestion


# --- eighth review: shared titles in the hint; REST counts code points (#831) --------------------


async def _seed_titled(db_session: AsyncSession, rows: list[tuple[str, dict]]) -> list[Document]:
    """Documents with the given (title, metadata) and the same chunk text, so a search ties them."""
    text = "The harbour pilot ledger records every tide, buoy and beacon. " * 6
    docs = []
    for title, meta in rows:
        doc = Document(
            title=title,
            status="active",
            sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
            pipeline_status=PipelineStatus.ready,
            mime_type="text/plain",
            doc_metadata=meta,
        )
        db_session.add(doc)
        await db_session.flush()
        db_session.add(Chunk(doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en", ocr_used=False))
        docs.append(doc)
    await db_session.flush()
    return docs


@pytest.mark.parametrize("who", ["key cap=None", "key cap=50"])
async def test_candidates_sharing_a_title_keep_their_own_hint_entries(
    db_session, mock_session_factory, admin_user, who
):
    """Mail subjects repeat ("Re: ..."): two candidates titled "Contract" must not overwrite each other.
    Colliding titles are labelled with the doc id's first 8 hex digits; the field stays offerable."""
    a, b, c = await _seed_titled(
        db_session,
        [
            ("Contract", {"sidecar": {"vendor": "Acme"}}),
            ("Contract", {"sidecar": {"vendor": "Beta"}}),
            ("Other", {"sidecar": {"vendor": "Gamma"}}),
        ],
    )
    hint = await _hint_as(db_session, admin_user, who)
    assert hint is not None
    shown = hint["differing_metadata"]["sidecar.vendor"]
    assert shown == {
        f"Contract [{str(a.doc_id)[:8]}]": "Acme",
        f"Contract [{str(b.doc_id)[:8]}]": "Beta",
        "Other": "Gamma",
    }, shown
    assert "metadata_filter={'sidecar.vendor':" in hint["suggestion"], hint["suggestion"]
    assert "(cut)" not in hint["suggestion"]


async def test_truncated_entries_name_the_right_document_when_titles_collide(
    db_session, mock_session_factory, admin_user
):
    docs = await _seed_titled(
        db_session,
        [("Contract", {"sidecar": {"vendor": f"{t} " + "v" * 300}}) for t in ("A", "B", "C")],
    )
    hint = await _hint_as(db_session, admin_user, "human")
    assert hint is not None
    named = {e["doc_id"] for e in hint["differing_metadata"][TRUNCATED_KEY] if "doc_id" in e}
    assert named == {str(d.doc_id) for d in docs}, hint["differing_metadata"][TRUNCATED_KEY]


async def test_verify_identifier_compares_candidates_sharing_a_title(db_session, mock_session_factory, admin_user):
    """Two "Contract" candidates with different vendors differ on the vendor; a per-title comparison
    used to collapse them into one value and report no difference."""
    import harbor_clerk.mcp_server as m

    await _seed_titled(
        db_session,
        [("Contract", {"sidecar": {"vendor": "Acme"}}), ("Contract", {"sidecar": {"vendor": "Beta"}})],
    )
    payload = await _mcp_as(db_session, admin_user, "key cap=None", lambda: m.kb_verify_identifier("Contract"))
    assert payload["status"] == "ambiguous", payload
    vendors = {c["discriminating_fields"].get("sidecar.vendor") for c in payload["candidates"]}
    assert vendors == {"Acme", "Beta"}, payload["candidates"]


_FRENCH_SUMMARY = ("Le gardien a consigné la tempête de l'été dernier. " * 30)[:1400]
_CJK_SUMMARY = ("港の灯台守は嵐の夜に灯を守った。" * 60)[:800]


@pytest.mark.parametrize("summary", [_FRENCH_SUMMARY, _CJK_SUMMARY], ids=["french-1400", "cjk-800"])
async def test_summary_bound_counts_each_surfaces_unit(client, db_session, mock_session_factory, admin_user, summary):
    """REST sends UTF-8 JSON, so its 1,500-character bound counts code points: these summaries come
    back whole. MCP sends escaped JSON (6 characters per non-ASCII one), so the same summaries are
    bounded there in escaped characters."""
    import harbor_clerk.mcp_server as m
    from harbor_clerk.payload_bounds import SUMMARY_MAX_CHARS, escaped_len

    doc = Document(
        title="Summary unit",
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="text/plain",
        summary=summary,
        doc_metadata={},
    )
    db_session.add(doc)
    await db_session.flush()
    assert len(summary) <= SUMMARY_MAX_CHARS < escaped_len(summary)
    raw = await _make_key(db_session, None)
    principal = await _resolve_principal(raw)
    token = _mcp_principal.set(principal)
    try:
        payload = json.loads(await m.kb_get_document(doc_id=str(doc.doc_id)))
    finally:
        _mcp_principal.reset(token)
    assert payload["summary"].endswith(ELLIPSIS), payload["summary"][-20:]
    assert escaped_len(payload["summary"]) <= SUMMARY_MAX_CHARS
    resp = await client.get(f"/api/docs/{doc.doc_id}", headers=auth_header(raw))
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["summary"] == summary, "REST cut a summary within 1,500 code points"


# --- ninth review: a refused filter is a 422 even when a scope check would answer empty (#831) ----


@pytest.mark.parametrize("path", ["key scope", "request scope"])
@pytest.mark.parametrize("surface", list(FILTER_REST))
async def test_rest_refuses_a_capped_filter_before_scope_early_returns(client, db_session, corpus, surface, path):
    """Both REST search routes answer an empty 200 when a scope leaves nothing visible; a capped key's
    refused metadata_filter must still get the documented 422, so the refusal comes first."""
    from harbor_clerk.models.watched import WatchedFolder

    empty = WatchedFolder(path="/empty-scope", display_name="Empty", auto_discovered=False)
    db_session.add(empty)
    await db_session.flush()
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            name="scoped-cap",
            key_hash=hash_api_key(raw),
            permission_tier="full",
            max_snippet_chars=50,
            scope_folder_ids=[str(empty.folder_id)] if path == "key scope" else None,
        )
    )
    await db_session.flush()
    route, body = FILTER_REST[surface]
    body = {**body, "metadata_filter": {"sidecar.vendor": _LONG_FILTER}}
    if path == "request scope":
        body["scope"] = {"folder_ids": [str(empty.folder_id)]}
    resp = await client.post(route, json=body, headers=auth_header(raw))
    assert resp.status_code == 422, resp.text[:300]
    assert "snippet cap" in resp.json()["detail"]
    # The same request without the refused filter takes the scope's empty 200: the path is real.
    body.pop("metadata_filter")
    resp = await client.post(route, json=body, headers=auth_header(raw))
    assert resp.status_code == 200, resp.text[:300]
    assert str(corpus.paged_doc.doc_id) not in resp.text


# --- tenth review: lists as filters, scoped early returns, Tika mail headers, citation bounds -------


@pytest.mark.parametrize("who", ["human", "key cap=None", "key cap=50"])
async def test_a_list_of_scalars_is_offered_as_a_filter(db_session, mock_session_factory, admin_user, who):
    """metadata_filter matches a list with @>, so a list of scalars shown whole is offerable, as on main."""
    await _seed_hint_pair(db_session, lambda t: {"frontmatter": {"tags": ["alpha", "beta"] if t == "A" else ["gamma"]}})
    hint = await _hint_as(db_session, admin_user, who)
    assert hint is not None
    suggestion = hint["suggestion"]
    assert "metadata_filter={'frontmatter.tags': [" in suggestion, suggestion
    assert "['alpha', 'beta']" in suggestion or "['gamma']" in suggestion, suggestion


async def test_a_list_cut_by_the_cap_is_not_offered(db_session, mock_session_factory, admin_user):
    """At cap 0 every string element shows as null: the list is not whole for that key."""
    await _seed_hint_pair(
        db_session,
        lambda t: {"frontmatter": {"tags": ["alpha", "beta"] if t == "A" else ["gamma"], "n": 1 if t == "A" else 2}},
    )
    hint = await _hint_as(db_session, admin_user, "key cap=0")
    assert hint is not None
    assert "frontmatter.tags" not in hint["suggestion"].split(" to ")[0], hint["suggestion"]
    assert "alpha" not in json.dumps(hint)


@pytest.mark.parametrize("path", ["empty scope", "doc_id outside", "doc_ids outside"])
@pytest.mark.parametrize("tool", ["kb_search", "kb_find_all"])
async def test_text_contains_scope_note_on_scoped_early_returns(
    db_session, corpus, mock_session_factory, admin_user, tool, path
):
    """A scoped capped key that sends text_contains gets the note on every response, including the
    early returns a scope takes before searching."""
    import harbor_clerk.mcp_server as m
    from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder

    scoped = WatchedFolder(path="/scoped", display_name="Scoped", auto_discovered=False)
    empty = WatchedFolder(path="/empty", display_name="Empty", auto_discovered=False)
    db_session.add_all([scoped, empty])
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=scoped.folder_id,
            doc_id=corpus.chunk_doc.doc_id,
            relative_path="notes.txt",
            sha256=b"d" * 32,
            bookmark_data=b"",
            status=WatchedFileStatus.active,
        )
    )
    raw = generate_api_key()
    folder = empty if path == "empty scope" else scoped
    db_session.add(
        ApiKey(
            name="scoped-tc",
            key_hash=hash_api_key(raw),
            permission_tier="full",
            max_snippet_chars=50,
            scope_folder_ids=[str(folder.folder_id)],
        )
    )
    await db_session.flush()
    kwargs: dict = {"text_contains": "lighthouse"}
    if path == "doc_id outside":
        kwargs["doc_id"] = str(corpus.paged_doc.doc_id)
    elif path == "doc_ids outside":
        kwargs["doc_ids"] = [str(corpus.paged_doc.doc_id)]
    query = "lighthouse keeper" if tool == "kb_search" else "lighthouse"
    principal = await _resolve_principal(raw)
    token = _mcp_principal.set(principal)
    try:
        payload = json.loads(await getattr(m, tool)(query=query, **kwargs))
    finally:
        _mcp_principal.reset(token)
    assert not payload.get("hits") and not payload.get("results"), payload
    assert "first 50 characters" in payload.get("text_contains_scope", ""), payload


@pytest.mark.parametrize("cap", [10, 0])
async def test_tika_mail_headers_are_outside_the_cap(db_session, corpus, mock_session_factory, admin_user, cap):
    """Legacy messages carry their headers in tika.email_*, which citations fall back to: like the
    email.* columns, those are envelope fields, shown whole and filterable under any cap."""
    import harbor_clerk.mcp_server as m

    tika = {
        "email_from": "Harbour Master <harbourmaster@port-authority.example.org>",
        "email_subject": "Pilotage notice for the North Basin, January sailings and tides",
        "email_to": ["pilots.on.duty@port-authority.example.org", "ops@port-authority.example.org"],
        "description": "Body text quoted in the description, which is document text and stays capped.",
    }
    doc = Document(
        title="Legacy message",
        status="active",
        sha256=uuid.uuid4().bytes + uuid.uuid4().bytes,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        doc_metadata={"tika": tika},
    )
    db_session.add(doc)
    await db_session.flush()
    who = f"key cap={cap}"
    payload = await _mcp_as(db_session, admin_user, who, lambda: m.kb_get_document(doc_id=str(doc.doc_id)))
    shown = payload["metadata"]["tika"]
    for key in ("email_from", "email_subject", "email_to"):
        assert shown[key] == tika[key], (key, shown[key])
    assert shown["description"] == (tika["description"][:cap] if cap > 0 else None)
    for metadata_filter in (
        {"tika.email_subject": tika["email_subject"]},
        {"tika.email_from": tika["email_from"]},
        {"tika.email_to": ["pilots.on.duty@port-authority.example.org"]},
    ):
        found = await _mcp_as(db_session, admin_user, who, lambda f=metadata_filter: FILTER_MCP["kb_search"](f))
        assert "error" not in found, (metadata_filter, found)
    refused = await _mcp_as(
        db_session, admin_user, who, lambda: FILTER_MCP["kb_search"]({"tika.description": tika["description"]})
    )
    assert "snippet cap" in refused.get("error", ""), refused


def test_a_citation_bounds_the_subject_and_display_name_at_the_rfc_line_limit():
    from harbor_clerk.source_ref import format_email_citation

    citation = format_email_citation(from_name="N" * 2_000, subject="S" * 3_000)
    sender = citation.removeprefix("Email from ").split(", ")[0]
    subject = citation.split(", ", 1)[1].strip('"')
    assert len(sender) == 998 and sender.endswith(ELLIPSIS)
    assert len(subject) == 998 and subject.endswith(ELLIPSIS)
    assert format_email_citation(from_name="Harbour Master", subject="Notice") == 'Email from Harbour Master, "Notice"'


async def test_verify_identifier_suggestion_is_bounded(db_session, mock_session_factory, admin_user):
    """Two candidates differing on 600 sidecar keys: the suggestion names fields while they fit, then
    "and M more", so the result the chat receives stays valid JSON at the tight-fit context."""
    import harbor_clerk.mcp_server as m
    from harbor_clerk.llm.chat import _tool_result_max_chars, _truncate_for_llm
    from harbor_clerk.llm.models import TIGHT_FIT_CONTEXT
    from harbor_clerk.payload_bounds import SUGGESTION_MAX_CHARS, escaped_len

    await _seed_titled(
        db_session,
        [("Contract", {"sidecar": {f"clause_field_{i:03d}": f"{t}{i}" for i in range(600)}}) for t in ("A", "B")],
    )
    token = _mcp_principal.set(Principal(type="user", id=admin_user.user_id, role="admin"))
    try:
        out = await m.kb_verify_identifier("Contract")
    finally:
        _mcp_principal.reset(token)
    payload = json.loads(out)
    assert payload["status"] == "ambiguous", payload
    suggestion = payload["suggestion"]
    assert escaped_len(suggestion) <= SUGGESTION_MAX_CHARS, escaped_len(suggestion)
    assert "more" in suggestion and "sidecar.clause_field" in suggestion, suggestion
    json.loads(_truncate_for_llm(out, _tool_result_max_chars(TIGHT_FIT_CONTEXT)))
