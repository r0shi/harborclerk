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
