"""API-key tier enforcement and path redaction on REST routes (#646).

MCP enforces a key's tier (``ScopedFastMCP.call_tool``) and never emits an
absolute on-disk path; the same key reaches REST. These tests hold REST to the
same contract: every key-reachable route is governed by an MCP tool
(``REST_ROUTE_TOOLS``), a key may call the route iff it may call the tool, and
no key — at any tier — receives an absolute path. Human sessions keep them.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harbor_clerk.api.deps import Principal
from harbor_clerk.api.schemas.documents import DocumentSummary
from harbor_clerk.api.scope import (
    FULL_TIER_TOOLS,
    REST_ROUTE_TOOLS,
    minimum_tier_for_tool,
    redact_path_tokens,
    redact_paths_for_principal,
)
from harbor_clerk.auth import generate_api_key, hash_api_key
from harbor_clerk.models import ApiKey, Chunk, Document, DocumentPage, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from harbor_clerk.search_types import FindAllHit, FindAllResult, SearchHit, SearchResult
from tests.conftest import auth_header

TIER_RANK = {"search": 0, "read": 1, "full": 2}

# Every route an API key may reach, as a client calls it. ``{doc_id}`` and
# ``{chunk_id}`` are filled from the corpus fixture; the expected minimum tier is
# derived from REST_ROUTE_TOOLS so this list exercises the table rather than
# restating it, and a test below checks the two cover each other exactly.
KEY_ROUTES: list[tuple[str, str, dict | None]] = [
    ("GET", "/api/docs", None),
    ("GET", "/api/docs/overview", None),
    ("GET", "/api/docs/filters", None),
    ("POST", "/api/search", {"query": "board packet"}),
    ("POST", "/api/search/find-all", {"query": "packet"}),
    ("POST", "/api/passages/read", {"chunk_ids": ["{chunk_id}"]}),
    ("GET", "/api/docs/{doc_id}", None),
    ("GET", "/api/docs/{doc_id}/outline", None),
    ("GET", "/api/docs/{doc_id}/content", None),
    ("GET", "/api/docs/{doc_id}/download", None),
    ("GET", "/api/docs/{doc_id}/related", None),
    ("GET", "/api/docs/{doc_id}/entities", None),
    ("GET", "/api/docs/entities/autocomplete?q=board", None),
    ("GET", "/api/docs/entities/top?entity_type=ORG", None),
]

# Read routes with no MCP counterpart: corpus-wide and unscoped (jobs, stats),
# carrying absolute folder paths (watch), or the web app's own surface (chat).
# They were open to any key before #646 and are human-only now.
HUMAN_ONLY_READS: list[tuple[str, str]] = [
    ("GET", "/api/jobs/active"),
    ("GET", "/api/jobs/snapshot"),
    ("GET", "/api/stats"),
    ("GET", "/api/stats/clusters"),
    ("GET", "/api/stats/entity-network"),
    ("GET", "/api/stats/topics"),
    ("GET", "/api/stats/timeline"),
    ("GET", "/api/docs/{doc_id}/stats"),
    ("GET", "/api/watch/folders"),
    ("GET", "/api/watch/system"),
    ("GET", "/api/watch/allowed-extensions"),
    ("GET", "/api/watch/folders/{folder_id}/progress"),
    ("GET", "/api/chat/conversations"),
    ("GET", "/api/chat/models"),
    ("GET", "/api/chat/models/status"),
    ("GET", "/api/research"),
    ("GET", "/api/research/active"),
]
HUMAN_ONLY_STREAMS = ("/jobs/stream", "/watch/folders/stream")


@dataclass
class Corpus:
    folder: WatchedFolder
    doc: Document
    chunk: Chunk
    source_path: str  # absolute, on disk, must never reach an API key


@pytest.fixture
async def corpus(db_session: AsyncSession, tmp_path) -> Corpus:
    folder_dir = tmp_path / "Secret Folder"
    (folder_dir / "board").mkdir(parents=True)
    src = folder_dir / "board" / "packet.pdf"
    src.write_bytes(b"%PDF-1.4 board packet")

    folder = WatchedFolder(path=str(folder_dir), display_name="Board", auto_discovered=False)
    db_session.add(folder)
    await db_session.flush()

    doc = Document(
        title="Board Packet",
        canonical_filename="packet.pdf",
        status="active",
        sha256=b"p" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
        source_path=str(src),
        doc_metadata={},
    )
    db_session.add(doc)
    await db_session.flush()

    text = "The board packet covers the budget."
    chunk = Chunk(
        doc_id=doc.doc_id, chunk_num=0, chunk_text=text, language="en", ocr_used=False, page_start=1, page_end=1
    )
    db_session.add_all(
        [
            WatchedFile(
                folder_id=folder.folder_id,
                relative_path="board/packet.pdf",
                bookmark_data=b"",
                sha256=doc.sha256,
                doc_id=doc.doc_id,
                status=WatchedFileStatus.active,
            ),
            DocumentPage(doc_id=doc.doc_id, page_num=1, page_text=text, ocr_used=False),
            chunk,
        ]
    )
    await db_session.flush()
    return Corpus(folder=folder, doc=doc, chunk=chunk, source_path=str(src))


@pytest.fixture
def make_key(db_session: AsyncSession):
    async def _make(tier: str, overrides: dict[str, bool] | None = None) -> str:
        raw = generate_api_key()
        db_session.add(
            ApiKey(
                name=f"tier-{tier}", key_hash=hash_api_key(raw), permission_tier=tier, tool_overrides=overrides or {}
            )
        )
        await db_session.flush()
        return raw

    return _make


@pytest.fixture
def downloads_enabled():
    """ALLOW_SOURCE_DOWNLOAD is off by default and 403s every caller; the tier
    tests need the download route to reach its tier check and succeed."""
    from harbor_clerk.config import get_settings

    get_settings().allow_source_download = True
    yield  # the autouse settings-singleton reset in conftest undoes this


def _template(path: str) -> str:
    """'/api/docs/{doc_id}/content?x=1' -> '/docs/{doc_id}/content', the REST_ROUTE_TOOLS key."""
    return path.split("?", 1)[0].removeprefix("/api")


async def _call(client, method: str, path: str, body: dict | None, headers: dict, corpus: Corpus):
    url = path.replace("{doc_id}", str(corpus.doc.doc_id)).replace("{folder_id}", str(corpus.folder.folder_id))
    if body is not None:
        body = json.loads(json.dumps(body).replace("{chunk_id}", str(corpus.chunk.chunk_id)))
    return await client.request(method, url, json=body, headers=headers)


def _dependency_names(route) -> set[str]:
    names: set[str] = set()

    def walk(dependant) -> None:
        name = getattr(dependant.call, "__name__", None)
        if name:
            names.add(name)
        for sub in dependant.dependencies:
            walk(sub)

    walk(route.dependant)
    return names


def _api_routes():
    from harbor_clerk.api.app import create_app
    from scripts.gen_docs.generators.rest import _walk_routes

    return list(_walk_routes(create_app()))


# --- tier enforcement -------------------------------------------------------


@pytest.mark.parametrize("tier", ["search", "read", "full"])
@pytest.mark.parametrize(("method", "path", "body"), KEY_ROUTES, ids=[f"{m} {p}" for m, p, _ in KEY_ROUTES])
async def test_key_route_is_governed_by_its_tool_tier(
    client, corpus, make_key, downloads_enabled, tier, method, path, body
) -> None:
    tool = REST_ROUTE_TOOLS[(method, _template(path))]
    required = minimum_tier_for_tool(tool)
    assert required is not None, tool

    resp = await _call(client, method, path, body, auth_header(await make_key(tier)), corpus)

    if TIER_RANK[tier] >= TIER_RANK[required]:
        assert resp.status_code < 400, (resp.status_code, resp.text[:300])
    else:
        assert resp.status_code == 403, (resp.status_code, resp.text[:300])
        assert tool in resp.json()["detail"]
        assert tier in resp.json()["detail"]


async def test_search_tier_key_gets_403_on_the_routes_the_issue_names(
    client, corpus, make_key, downloads_enabled
) -> None:
    """#646 verbatim: detail, content, download and passages/read were open to a search-tier key."""
    headers = auth_header(await make_key("search"))
    doc_id = corpus.doc.doc_id
    responses = [
        await client.get(f"/api/docs/{doc_id}", headers=headers),
        await client.get(f"/api/docs/{doc_id}/content", headers=headers),
        await client.get(f"/api/docs/{doc_id}/download", headers=headers),
        await client.post("/api/passages/read", json={"chunk_ids": [str(corpus.chunk.chunk_id)]}, headers=headers),
    ]
    assert [r.status_code for r in responses] == [403, 403, 403, 403]
    for r in responses:
        assert "source_path" not in r.text
        assert corpus.source_path not in r.text


async def test_tool_overrides_grant_and_revoke_rest_routes_as_they_do_mcp_tools(client, corpus, make_key) -> None:
    doc_id = corpus.doc.doc_id

    search_plus_detail = auth_header(await make_key("search", {"kb_get_document": True}))
    assert (await client.get(f"/api/docs/{doc_id}", headers=search_plus_detail)).status_code == 200
    assert (await client.get(f"/api/docs/{doc_id}/content", headers=search_plus_detail)).status_code == 403

    full_minus_read = auth_header(await make_key("full", {"kb_read_document": False}))
    assert (await client.get(f"/api/docs/{doc_id}/content", headers=full_minus_read)).status_code == 403
    assert (await client.get(f"/api/docs/{doc_id}", headers=full_minus_read)).status_code == 200


async def test_key_without_scope_is_refused_not_admitted(client, corpus) -> None:
    """A Principal of type api_key with no KeyScope cannot happen through
    get_current_principal; if it ever does, the gate must fail closed."""
    from harbor_clerk.api.deps import get_current_principal

    app = client._transport.app  # the create_app() instance the fixture built

    async def _scopeless_key():
        return Principal(type="api_key", id=uuid.uuid4(), role="user", key_scope=None)

    app.dependency_overrides[get_current_principal] = _scopeless_key
    try:
        resp = await client.get("/api/docs", headers=auth_header("hc_irrelevant"))
    finally:
        del app.dependency_overrides[get_current_principal]
    assert resp.status_code == 403


# --- path redaction ---------------------------------------------------------


async def test_human_session_keeps_absolute_paths(client, corpus, admin_user, admin_token) -> None:
    listing = await client.get("/api/docs", headers=auth_header(admin_token))
    assert listing.status_code == 200
    (item,) = listing.json()["items"]
    assert item["source_path"] == corpus.source_path
    assert item["watch_source_path"] == corpus.source_path  # folder.path + "/" + relative_path

    detail = await client.get(f"/api/docs/{corpus.doc.doc_id}", headers=auth_header(admin_token))
    assert detail.status_code == 200
    assert detail.json()["source_path"] == corpus.source_path


@pytest.mark.parametrize("tier", ["read", "full"])
async def test_key_never_receives_absolute_paths_in_document_shapes(client, corpus, make_key, tier) -> None:
    headers = auth_header(await make_key(tier))

    listing = await client.get("/api/docs", headers=headers)
    assert listing.status_code == 200
    (item,) = listing.json()["items"]
    assert item["source_path"] is None
    assert item["watch_source_path"] is None
    # Labels survive: a folder's display name and the file's own name are not paths.
    assert item["folder_name"] == "Board"
    assert item["canonical_filename"] == "packet.pdf"
    assert corpus.source_path not in listing.text
    assert corpus.folder.path not in listing.text

    detail = await client.get(f"/api/docs/{corpus.doc.doc_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["source_path"] is None
    assert corpus.source_path not in detail.text
    assert corpus.folder.path not in detail.text


@pytest.mark.parametrize(("method", "path", "body"), KEY_ROUTES, ids=[f"{m} {p}" for m, p, _ in KEY_ROUTES])
async def test_no_key_reachable_route_leaks_the_folder_path(
    client, corpus, make_key, downloads_enabled, method, path, body
) -> None:
    """The sweep: every route a full key reaches, body and headers, with the
    on-disk folder path as the needle (it prefixes source_path too)."""
    resp = await _call(client, method, path, body, auth_header(await make_key("full")), corpus)
    assert resp.status_code < 400, (resp.status_code, resp.text[:300])
    assert corpus.folder.path not in resp.text
    assert corpus.folder.path not in str(dict(resp.headers))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "OSError: [Errno 2] No such file or directory: '/Users/alex/Secret Folder/board/packet.pdf'",
            "OSError: [Errno 2] No such file or directory: [path]",
        ),
        ('Cannot open "/Volumes/Archive/2024 Q3/minutes.docx" for reading', "Cannot open [path] for reading"),
        ("Tika failed for /data/watch/a.pdf (HTTP 500)", "Tika failed for [path] (HTTP 500)"),
        ("home ~/Documents/x.pdf missing", "home [path] missing"),
        ("fetch https://tika.local:9998/tika timed out", "fetch https://tika.local:9998/tika timed out"),
        ("ratio 1/2 and a / b and a/b", "ratio 1/2 and a / b and a/b"),
        ("RuntimeError", "RuntimeError"),
    ],
)
def test_redact_path_tokens(text: str, expected: str) -> None:
    assert redact_path_tokens(text) == expected


async def test_key_reads_error_text_with_paths_redacted_human_reads_it_whole(
    client, db_session, corpus, make_key, admin_user, admin_token
) -> None:
    """A stage stores describe_error(exc); an OSError quotes the file. The key
    keeps the message (what failed) and loses the path; the human keeps both."""
    message = f"OSError: [Errno 2] No such file or directory: '{corpus.source_path}'"
    corpus.doc.error = message
    db_session.add(
        IngestionJob(
            doc_id=corpus.doc.doc_id,
            stage=JobStage.extract,
            status=JobStatus.error,
            pipeline_seq=corpus.doc.pipeline_seq,
            error=message,
        )
    )
    await db_session.flush()
    url = f"/api/docs/{corpus.doc.doc_id}"

    human = (await client.get(url, headers=auth_header(admin_token))).json()
    assert human["error"] == message
    assert human["jobs"][0]["error"] == message

    resp = await client.get(url, headers=auth_header(await make_key("read")))
    key = resp.json()
    assert key["error"] == "OSError: [Errno 2] No such file or directory: [path]"
    assert key["jobs"][0]["error"] == "OSError: [Errno 2] No such file or directory: [path]"
    assert corpus.folder.path not in resp.text


async def test_failed_document_explains_its_error_and_keys_still_get_no_path(
    client, db_session, corpus, make_key, admin_user, admin_token
) -> None:
    """#555: the list and detail carry the raw error plus a plain-language
    error_info. The explanation is fixed text, so it holds for a key too, while
    the raw error keeps its path redaction on both routes."""
    message = f"PermissionError: [Errno 13] Permission denied: '{corpus.source_path}'"
    corpus.doc.pipeline_status = PipelineStatus.error
    corpus.doc.error = message
    db_session.add(
        IngestionJob(
            doc_id=corpus.doc.doc_id,
            stage=JobStage.extract,
            status=JobStatus.error,
            pipeline_seq=corpus.doc.pipeline_seq,
            error=message,
        )
    )
    await db_session.flush()
    detail_url = f"/api/docs/{corpus.doc.doc_id}"

    (human_item,) = (await client.get("/api/docs", headers=auth_header(admin_token))).json()["items"]
    human_detail = (await client.get(detail_url, headers=auth_header(admin_token))).json()
    assert human_item["error"] == message
    for info in (human_item["error_info"], human_detail["error_info"], human_detail["jobs"][0]["error_info"]):
        assert info["family"] == "unreadable"
        assert info["message"] and info["action"]

    key_headers = auth_header(await make_key("read"))
    listing = await client.get("/api/docs", headers=key_headers)
    detail = await client.get(detail_url, headers=key_headers)
    (key_item,) = listing.json()["items"]
    assert key_item["error"] == "PermissionError: [Errno 13] Permission denied: [path]"
    assert key_item["error_info"] == human_item["error_info"]
    assert detail.json()["error_info"] == human_detail["error_info"]
    for resp in (listing, detail):
        assert corpus.folder.path not in resp.text


async def test_a_ready_document_carries_no_error_in_the_list(client, db_session, corpus, admin_token) -> None:
    """A stale error left on a document that has since become ready is not shown as a failure."""
    corpus.doc.error = "ConnectError: [Errno 61] Connection refused"
    await db_session.flush()
    (item,) = (await client.get("/api/docs", headers=auth_header(admin_token))).json()["items"]
    assert item["error"] is None
    assert item["error_info"] is None


async def test_a_ready_document_is_not_explained_as_failed_in_the_detail(
    client, db_session, corpus, make_key, admin_token
) -> None:
    """The detail follows the list's rule for error_info; its raw `error` is unchanged
    (still the stored text, path-redacted for a key)."""
    stale = f"PermissionError: [Errno 13] Permission denied: '{corpus.source_path}'"
    corpus.doc.error = stale
    await db_session.flush()
    url = f"/api/docs/{corpus.doc.doc_id}"

    human = (await client.get(url, headers=auth_header(admin_token))).json()
    assert human["error_info"] is None
    assert human["error"] == stale

    resp = await client.get(url, headers=auth_header(await make_key("read")))
    assert resp.json()["error_info"] is None
    assert resp.json()["error"] == "PermissionError: [Errno 13] Permission denied: [path]"
    assert corpus.folder.path not in resp.text


async def test_a_skipped_job_with_a_reason_is_not_explained_as_a_failure(
    client, db_session, corpus, admin_token
) -> None:
    """A skipped stage reports its reason in `error`; that is not a failure to explain."""
    db_session.add(
        IngestionJob(
            doc_id=corpus.doc.doc_id,
            stage=JobStage.entities,
            status=JobStatus.done,
            pipeline_seq=corpus.doc.pipeline_seq,
            metrics={"skipped": True, "reason": "spacy_unavailable"},
        )
    )
    await db_session.flush()
    detail = (await client.get(f"/api/docs/{corpus.doc.doc_id}", headers=auth_header(admin_token))).json()
    (job,) = detail["jobs"]
    assert job["status"] == "skipped"
    assert job["error"] == "spaCy NER models not available"
    assert job["error_info"] is None


def test_redact_paths_blanks_nested_fields_for_keys_only() -> None:
    now = datetime.now(UTC)

    def payload() -> dict:
        return {
            "items": [
                DocumentSummary(
                    doc_id="d",
                    title="t",
                    status="active",
                    created_at=now,
                    updated_at=now,
                    source_path="/a",
                    watch_source_path="/b",
                )
            ],
            "folder": {"source_path": "/c", "nested": [{"watch_source_path": "/d", "keep": "/not-a-path-field"}]},
        }

    human = Principal(type="user", id=uuid.uuid4(), role="admin")
    kept = redact_paths_for_principal(payload(), human)
    assert kept["items"][0].source_path == "/a" and kept["folder"]["nested"][0]["watch_source_path"] == "/d"

    key = Principal(type="api_key", id=uuid.uuid4(), role="user")
    blanked = redact_paths_for_principal(payload(), key)
    assert blanked["items"][0].source_path is None
    assert blanked["items"][0].watch_source_path is None
    assert blanked["items"][0].title == "t"
    assert blanked["folder"]["source_path"] is None
    assert blanked["folder"]["nested"][0] == {"watch_source_path": None, "keep": "/not-a-path-field"}


# --- the table and the routes stay in step ----------------------------------


def test_rest_route_tools_table_matches_the_routes_that_admit_keys() -> None:
    """Every route that uses require_read_access has an entry, and every entry
    names a real route — so neither an unlisted route (closed to keys without
    anyone deciding so) nor a stale entry can hide."""
    admits: set[tuple[str, str]] = set()
    for route in _api_routes():
        if "require_read_access" in _dependency_names(route):
            admits |= {(method, route.path) for method in route.methods}
    assert admits == set(REST_ROUTE_TOOLS), {
        "routes without an entry": admits - set(REST_ROUTE_TOOLS),
        "entries without a route": set(REST_ROUTE_TOOLS) - admits,
    }
    assert set(REST_ROUTE_TOOLS.values()) <= FULL_TIER_TOOLS
    assert {(m, _template(p)) for m, p, _ in KEY_ROUTES} == set(REST_ROUTE_TOOLS)


def test_streams_with_no_mcp_counterpart_are_human_only() -> None:
    by_path = {route.path: _dependency_names(route) for route in _api_routes()}
    for path in HUMAN_ONLY_STREAMS:
        assert "require_human_user" in by_path[path], path


@pytest.mark.parametrize(("method", "path"), HUMAN_ONLY_READS, ids=[f"{m} {p}" for m, p in HUMAN_ONLY_READS])
async def test_full_key_is_refused_on_reads_with_no_mcp_counterpart(client, corpus, make_key, method, path) -> None:
    resp = await _call(client, method, path, None, auth_header(await make_key("full")), corpus)
    assert resp.status_code == 403, (resp.status_code, resp.text[:300])


async def test_admin_session_still_reaches_the_human_only_reads(client, corpus, admin_user, admin_token) -> None:
    for path in ("/api/jobs/active", "/api/stats/topics", "/api/watch/folders", "/api/watch/system"):
        resp = await client.get(path, headers=auth_header(admin_token))
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
    conversations = await client.get("/api/chat/conversations", headers=auth_header(admin_token))
    assert conversations.status_code == 200


async def test_key_gets_no_folder_label_derived_from_the_on_disk_path(
    client, db_session, corpus, make_key, admin_user, admin_token
) -> None:
    """Without a display name the label falls back to the folder path's last
    component — a path component, so a key gets none; a human still does."""
    corpus.folder.display_name = None
    await db_session.flush()

    human = await client.get("/api/docs", headers=auth_header(admin_token))
    assert human.json()["items"][0]["folder_name"] == "Secret Folder"

    key = await client.get("/api/docs", headers=auth_header(await make_key("full")))
    (item,) = key.json()["items"]
    assert item["folder_name"] is None
    assert "Secret Folder" not in key.text


async def test_key_gets_no_folder_label_derived_from_the_on_disk_path_in_source_refs(
    client, db_session, corpus, make_key, admin_token, monkeypatch
) -> None:
    """The same rule for the SourceRef the search and passage routes attach to
    every hit (``folder_label``); a human session keeps the leaf."""
    corpus.folder.display_name = None
    await db_session.flush()

    async def fake_hybrid_search(*args, **kwargs):
        return SearchResult(
            hits=[
                SearchHit(
                    chunk_id=str(corpus.chunk.chunk_id),
                    doc_id=str(corpus.doc.doc_id),
                    chunk_num=0,
                    chunk_text=corpus.chunk.chunk_text,
                    page_start=1,
                    page_end=1,
                    language="en",
                    ocr_used=False,
                    ocr_confidence=None,
                    score=0.9,
                    doc_title="Board Packet",
                )
            ],
            total_candidates=1,
            reranker_status="disabled",
        )

    async def fake_find_all(*args, **kwargs):
        return FindAllResult(
            hits=[
                FindAllHit(
                    doc_id=str(corpus.doc.doc_id),
                    doc_title="Board Packet",
                    mime_type="application/pdf",
                    language="en",
                    score=0.9,
                    ingested_at=datetime.now(UTC),
                    page_range="1",
                    top_chunk_id=str(corpus.chunk.chunk_id),
                    top_chunk_text=corpus.chunk.chunk_text,
                    top_chunk_page=1,
                    top_chunk_heading=None,
                )
            ],
            total_matches=1,
            offset=0,
            truncated=False,
            sort_by="relevance",
            presentation="full",
        )

    monkeypatch.setattr("harbor_clerk.api.routes.search.hybrid_search", fake_hybrid_search)
    monkeypatch.setattr("harbor_clerk.api.routes.search.find_all", fake_find_all)
    # Every route that attaches a SourceRef, with the key its rows sit under.
    routes = (
        ("/api/search", {"query": "board packet"}, "hits"),
        ("/api/search/find-all", {"query": "packet"}, "results"),
        ("/api/passages/read", {"chunk_ids": [str(corpus.chunk.chunk_id)]}, "passages"),
    )

    human = auth_header(admin_token)
    for path, body, rows in routes:
        resp = await client.post(path, json=body, headers=human)
        assert resp.status_code == 200, (path, resp.text[:300])
        (row,) = resp.json()[rows]
        assert row["source"]["folder_label"] == "Secret Folder", path

    key = auth_header(await make_key("full"))
    for path, body, rows in routes:
        resp = await client.post(path, json=body, headers=key)
        assert resp.status_code == 200, (path, resp.text[:300])
        (row,) = resp.json()[rows]
        assert row["source"].get("folder_label") is None, path
        assert "Secret Folder" not in resp.text, path


# --- audit log --------------------------------------------------------------


async def _logged_rest_rows(db_session: AsyncSession, _engine, raw_key: str, path: str):
    """GET ``path`` with ``raw_key`` through the real audit middleware; return (response, log rows)."""
    from harbor_clerk.api.app import create_app
    from harbor_clerk.api.middleware import ApiKeyRequestLogMiddleware
    from harbor_clerk.db import get_session
    from harbor_clerk.models.api_request_log import ApiRequestLog

    await db_session.commit()  # the middleware resolves the key through its own session
    app = create_app()

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    with patch("harbor_clerk.db.async_session_factory", factory):
        transport = ASGITransport(app=ApiKeyRequestLogMiddleware(app))
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.get(path, headers=auth_header(raw_key))

    async with factory() as fresh:
        rows = (await fresh.execute(select(ApiRequestLog).where(ApiRequestLog.request_type == "rest"))).scalars().all()
    return resp, rows


async def test_rest_tier_denial_is_logged_as_denied(db_session: AsyncSession, _engine, corpus, make_key) -> None:
    """MCP logs an out-of-tier call as status 'denied'; the REST middleware must
    too, instead of a generic HTTP 403 error, so the dashboard counts them alike."""
    raw = await make_key("search")
    resp, rows = await _logged_rest_rows(db_session, _engine, raw, f"/api/docs/{corpus.doc.doc_id}")
    assert resp.status_code == 403
    assert [r.status for r in rows] == ["denied"]
    assert rows[0].endpoint == "GET /api/docs/{id}"
    assert "kb_get_document" in (rows[0].status_detail or "")


async def test_rest_human_only_refusal_is_logged_as_denied(db_session: AsyncSession, _engine, corpus, make_key) -> None:
    """A key on a human-only read is a denial too, not an error — the dashboard's
    denied count would otherwise miss every jobs/stats/watch/chat probe."""
    raw = await make_key("full")
    resp, rows = await _logged_rest_rows(db_session, _engine, raw, "/api/jobs/active")
    assert resp.status_code == 403
    assert [r.status for r in rows] == ["denied"]
    assert rows[0].endpoint == "GET /api/jobs/active"
    assert "API keys" in (rows[0].status_detail or "")
