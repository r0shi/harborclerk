"""The session helpers, driven offline with a fake client.

These are the paths a live failure would otherwise be the first test of: the
wipe guards, the cleanup that must run whatever failed, the document mapping,
and the two readiness predicates. AGENTS.md asks for cleanup and error paths
to be mutation-verified; the PR records the results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from acceptance import conftest
from acceptance.config import WIPE_MAX_DOCUMENTS, AcceptanceConfig
from acceptance.hc_client import HarborClerk, document_ready, folder_ingest_done, folder_stage_errors


def _cfg(tmp_path: Path, **over: Any) -> AcceptanceConfig:
    base = dict(
        api_base="http://localhost:8100",
        username="a@b.c",
        password="x",
        folder_root=tmp_path,
        folder_root_in_instance=None,
        insecure=False,
        disposable=False,
        wipe=False,
        keep=False,
        config_json=None,
        allow_model_swap=False,
        run_id="offline",
        ingest_timeout_s=5,
        ask_timeout_s=5,
    )
    base.update(over)
    return AcceptanceConfig(**base)


class FakeAdmin:
    def __init__(
        self,
        *,
        count: int = 0,
        folders: list[dict[str, Any]] | None = None,
        refuse_delete: tuple[str, ...] = (),
        ingest_error: Exception | None = None,
        create_error: Exception | None = None,
        items: list[dict[str, Any]] | None = None,
        delete_error: Exception | None = None,
        page_items: list[list[dict[str, Any]]] | None = None,
    ):
        self.delete_error = delete_error
        self.page_items = page_items  # per-offset pages for documents_under paging tests
        self.list_calls: list[dict[str, Any]] = []
        self.count = count
        self.folders = folders or []
        self.refuse_delete = refuse_delete
        self.ingest_error = ingest_error
        self.create_error = create_error
        self.items = items or []
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.requests: list[tuple[str, str]] = []

    def document_count(self) -> int:
        return self.count

    def folder_list(self) -> list[dict[str, Any]]:
        return self.folders

    def folder_create(self, path: str, *, recursive: bool = True) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        self.created.append(path)
        return {"folder_id": "f1", "path": path}

    def folder_delete(self, folder_id: str) -> None:
        if self.delete_error:
            raise self.delete_error
        if folder_id in self.refuse_delete:
            raise httpx.HTTPStatusError(
                "409", request=httpx.Request("DELETE", "http://x"), response=httpx.Response(409)
            )
        self.deleted.append(folder_id)

    def wait_for_folder_ingest(self, folder_id: str, *, expected_files: int, timeout_s: float, poll_s: float = 3.0):
        if self.ingest_error:
            raise self.ingest_error
        return {"completed_files": expected_files}

    def list_documents(self, **params: Any) -> dict[str, Any]:
        self.list_calls.append(params)
        if self.page_items is not None:
            n = params.get("offset", 0) // params.get("limit", 200)
            items = self.page_items[n] if n < len(self.page_items) else []
            return {"items": items, "total": sum(len(p) for p in self.page_items)}
        return {"items": self.items, "total": len(self.items)}

    def watch_system(self) -> dict[str, Any]:
        return {"platform": "macos"}

    def status_summary(self) -> dict[str, Any]:
        return {"state": "processing", "counts": {}, "needs_attention": []}

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self.requests.append((method, path))
        return httpx.Response(200, request=httpx.Request(method, "http://x" + path))


# ── wipe guards ─────────────────────────────────────────────────────────────


def test_wipe_refuses_without_the_disposable_flag(tmp_path: Path) -> None:
    admin = FakeAdmin()
    with pytest.raises(pytest.fail.Exception, match="DISPOSABLE"):
        conftest.wipe_instance(admin, _cfg(tmp_path, wipe=True, disposable=False))
    assert admin.requests == []


def test_wipe_refuses_an_instance_holding_more_than_the_cap(tmp_path: Path) -> None:
    admin = FakeAdmin(count=WIPE_MAX_DOCUMENTS + 1, folders=[{"folder_id": "f1", "path": "/a"}])
    with pytest.raises(pytest.fail.Exception, match="refusing to wipe"):
        conftest.wipe_instance(admin, _cfg(tmp_path, wipe=True, disposable=True))
    assert admin.deleted == [] and admin.requests == []


def test_wipe_stops_before_documents_when_a_folder_refuses_deletion(tmp_path: Path) -> None:
    admin = FakeAdmin(
        folders=[{"folder_id": "f1", "path": "/a"}, {"folder_id": "f2", "path": "/b"}], refuse_delete=("f2",)
    )
    with pytest.raises(pytest.fail.Exception, match="could not be deleted"):
        conftest.wipe_instance(admin, _cfg(tmp_path, wipe=True, disposable=True))
    assert admin.deleted == ["f1"]
    assert ("POST", "/api/system/delete-all-documents") not in admin.requests


def test_wipe_deletes_folders_then_documents(tmp_path: Path) -> None:
    admin = FakeAdmin(count=3, folders=[{"folder_id": "f1", "path": "/a"}])
    conftest.wipe_instance(admin, _cfg(tmp_path, wipe=True, disposable=True))
    assert admin.deleted == ["f1"]
    assert admin.requests == [("POST", "/api/system/delete-all-documents")]


# ── corpus session ──────────────────────────────────────────────────────────


def _items_for(folder: Path) -> list[dict[str, Any]]:
    return [
        {"doc_id": "d-lease", "watch_source_path": str(folder / "lease-agreement.pdf")},
        {"doc_id": "d-other", "watch_source_path": "/somewhere/else/other.pdf"},
    ]


def test_corpus_session_renders_before_registering_and_cleans_up(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    admin = FakeAdmin(items=_items_for(cfg.folder_path))
    with conftest.corpus_session(admin, cfg) as corpus:
        assert admin.created == [str(cfg.folder_path)]
        assert (cfg.folder_path / "notes.xyz").exists(), "fixtures must be on disk before the folder is registered"
        assert corpus.folder_id == "f1" and corpus.doc_id("lease-agreement.pdf") == "d-lease"
        assert "other.pdf" not in corpus.docs
        assert corpus.source_digests["lease-agreement.pdf"]
    assert admin.deleted == ["f1"], "the watched folder must be removed at the end of the session"
    assert not cfg.folder_path.exists(), "the rendered files must be removed at the end of the session"


def test_corpus_session_cleans_up_when_the_ingest_wait_fails(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    admin = FakeAdmin(ingest_error=TimeoutError("stalled"))
    with pytest.raises(pytest.fail.Exception, match="stalled"), conftest.corpus_session(admin, cfg):
        pass
    assert admin.deleted == ["f1"], "a failed ingest must not leave a registered folder behind"
    assert not cfg.folder_path.exists()


def test_corpus_session_cleans_the_directory_when_registration_fails(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    admin = FakeAdmin(create_error=RuntimeError("409 overlapping"))
    with pytest.raises(RuntimeError, match="overlapping"), conftest.corpus_session(admin, cfg):
        pass
    assert admin.deleted == [], "nothing was registered, nothing to delete"
    assert not cfg.folder_path.exists()


def test_corpus_session_keep_leaves_everything_in_place(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    admin = FakeAdmin(items=_items_for(cfg.folder_path))
    with conftest.corpus_session(admin, cfg, keep=True):
        pass
    assert admin.deleted == [] and cfg.folder_path.exists()


def test_corpus_session_wipes_first_when_asked(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, wipe=True, disposable=True)
    admin = FakeAdmin(count=1, folders=[{"folder_id": "old", "path": "/old"}], items=_items_for(cfg.folder_path))
    with conftest.corpus_session(admin, cfg):
        pass
    assert admin.requests[0] == ("POST", "/api/system/delete-all-documents")
    assert admin.deleted == ["old", "f1"]


def test_corpus_session_removes_the_files_even_when_the_folder_delete_fails(tmp_path: Path) -> None:
    """A 401 after a long run must not leave rendered files behind as well."""
    cfg = _cfg(tmp_path)
    admin = FakeAdmin(items=_items_for(cfg.folder_path), delete_error=RuntimeError("401 Token expired"))
    with pytest.raises(RuntimeError, match="Token expired"), conftest.corpus_session(admin, cfg):
        pass
    assert not cfg.folder_path.exists()


def test_documents_under_maps_by_relative_path(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    admin = FakeAdmin(items=_items_for(folder) + [{"doc_id": "d-src", "source_path": str(folder / "x.txt")}])
    mapped = conftest.documents_under(admin, str(folder))
    assert mapped == {"lease-agreement.pdf": admin.items[0], "x.txt": admin.items[2]}


def test_documents_under_pages_until_every_expected_name_is_found(tmp_path: Path) -> None:
    """The list is recency-ordered with no folder filter; on a busy instance
    our documents may not all sit on the first page."""
    folder = tmp_path / "f"
    ours = lambda name: {"doc_id": f"d-{name}", "watch_source_path": str(folder / name)}  # noqa: E731
    noise = [{"doc_id": f"n{i}", "watch_source_path": f"/elsewhere/{i}.pdf"} for i in range(2)]
    admin = FakeAdmin(page_items=[noise + [ours("a.txt")], noise + [ours("b.txt")], [ours("c.txt")]])
    mapped = conftest.documents_under(admin, str(folder), expected={"a.txt", "b.txt"}, page=3)
    assert set(mapped) == {"a.txt", "b.txt"}, "stops once every expected name is found"
    assert [c["offset"] for c in admin.list_calls] == [0, 3]
    admin2 = FakeAdmin(page_items=[noise + [ours("a.txt")], noise + [ours("b.txt")], [ours("c.txt")]])
    assert set(conftest.documents_under(admin2, str(folder), page=3)) == {"a.txt", "b.txt", "c.txt"}


def test_key_cleanup_reports_the_ids_it_could_not_delete() -> None:
    class Admin:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_api_key(self, name: str, *, permission_tier: str = "full", **fields: Any) -> dict[str, Any]:
            return {"key_id": name, "raw_key": "hc_x"}

        def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
            self.calls.append(path)
            status = 500 if path.endswith("-bad") else 204  # key ids here are the factory's generated names
            return httpx.Response(status, request=httpx.Request(method, "http://x" + path))

    admin = Admin()
    factory = conftest.KeyFactory(admin, "run")  # type: ignore[arg-type]
    factory.create("bad")  # first, so stopping at the first failure would skip the next one
    factory.create("good")
    assert factory.cleanup() == ["acceptance-run-bad"]
    assert len(admin.calls) == 2, "every key is attempted even after one fails"


# ── readiness predicates ────────────────────────────────────────────────────


def _progress(*, scan: str = "idle", completed: int = 8, **in_flight: int) -> dict[str, Any]:
    by_stage = {
        s: {"pending": 0, "running": 0, "done": 8, "error": 0}
        for s in ("extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize")
    }
    for stage, n in in_flight.items():
        by_stage[stage]["pending"] = n
    return {"scan_status": scan, "ingest_status": "idle", "completed_files": completed, "by_stage": by_stage}


def test_folder_ingest_done_ignores_summarize_but_not_foreground_stages() -> None:
    assert folder_ingest_done(_progress(), 8)
    assert folder_ingest_done(_progress(summarize=8), 8), "summarize is background and must not gate"
    assert not folder_ingest_done(_progress(embed=1), 8)
    assert not folder_ingest_done(_progress(scan="scanning"), 8)
    assert not folder_ingest_done(_progress(completed=7), 8)


def test_document_ready_needs_every_foreground_stage_done() -> None:
    jobs = [{"stage": s, "status": "done"} for s in ("extract", "chunk", "embed", "finalize")]
    assert document_ready({"pipeline_status": "ready", "jobs": jobs})
    assert document_ready({"pipeline_status": "ready", "jobs": jobs + [{"stage": "summarize", "status": "queued"}]})
    assert not document_ready({"pipeline_status": "ready", "jobs": jobs[:-1]})
    assert not document_ready({"pipeline_status": "processing", "jobs": jobs})


def test_folder_stage_errors_reports_foreground_errors_only() -> None:
    progress = _progress()
    assert folder_stage_errors(progress) == {}
    progress["by_stage"]["summarize"]["error"] = 3
    assert folder_stage_errors(progress) == {}, "summarize failures are background and must not fail the wait"
    progress["by_stage"]["extract"]["error"] = 1
    assert folder_stage_errors(progress) == {"extract": 1}


def test_wait_for_folder_ingest_fails_fast_on_an_errored_stage() -> None:
    class Client(HarborClerk):
        def folder_progress(self, folder_id: str) -> dict[str, Any]:
            p = _progress(completed=7)
            p["by_stage"]["extract"]["error"] = 1
            return p

    with Client("http://localhost:1") as client, pytest.raises(RuntimeError, match="errored stages"):
        client.wait_for_folder_ingest("f1", expected_files=8, timeout_s=2, poll_s=0.1)


def test_user_session_refreshes_once_on_401_and_retries() -> None:
    """The access token lives 30 minutes; a run with OCR can outlast it, and
    the teardown must not be the call that dies."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path} {request.headers.get('Authorization')}")
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"access_token": "t1", "token_type": "bearer", "user": {"role": "admin"}})
        if request.url.path == "/api/auth/refresh":
            return httpx.Response(200, json={"access_token": "t2", "token_type": "bearer"})
        if request.headers.get("Authorization") == "Bearer t1":
            return httpx.Response(401, json={"detail": "Token expired"})
        return httpx.Response(200, json={"status": "healthy", "checks": {}})

    with HarborClerk("http://test", transport=httpx.MockTransport(handler)) as client:
        client.login("a@b.c", "x")
        assert client.health()["status"] == "healthy"
        assert client.refreshes == 1
        assert calls[-3:] == [
            "GET /api/system/health Bearer t1",
            "POST /api/auth/refresh Bearer t1",
            "GET /api/system/health Bearer t2",
        ]


def test_api_key_client_never_tries_to_refresh() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"detail": "expired"})

    with HarborClerk("http://test", bearer="hc_key", transport=httpx.MockTransport(handler)) as client:
        assert client.request("GET", "/api/docs").status_code == 401
    assert calls == ["/api/docs"], "a key has no refresh cookie; retrying would only repeat the 401"


def test_wait_for_document_ready_detects_the_error_status_immediately() -> None:
    class Client(HarborClerk):
        def get_document(self, doc_id: str) -> dict[str, Any]:
            return {"doc_id": doc_id, "pipeline_status": "error", "error": "boom", "jobs": []}

    with Client("http://localhost:1") as client, pytest.raises(RuntimeError, match="boom"):
        client.wait_for_document_ready("d1", timeout_s=2, poll_s=0.1)


def test_stream_ask_enforces_a_total_budget_not_a_per_event_gap() -> None:
    """A tool-calling answer streams an event every second or so and could
    otherwise run for as long as the model likes; the budget is for the
    whole answer."""
    import time as _time

    def slow_events():
        for i in range(50):
            _time.sleep(0.05)
            yield f'data: {{"type": "tool_call", "n": {i}}}\n\n'.encode()

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield from slow_events()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowStream())
        return httpx.Response(200, json={})

    with HarborClerk("http://test", transport=httpx.MockTransport(handler)) as client:
        started = _time.monotonic()
        with pytest.raises(TimeoutError, match="exceeded 0s|exceeded"):
            client.stream_ask("c1", "q", timeout_s=0.3)
        assert _time.monotonic() - started < 2.0, "the budget must cut the stream short"


def test_stream_ask_returns_events_through_done() -> None:
    body = b'data: {"type": "text", "content": "hi"}\n\ndata: {"type": "done", "rag_context": {"citations": []}}\n\ndata: {"type": "after"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    with HarborClerk("http://test", transport=httpx.MockTransport(handler)) as client:
        events = client.stream_ask("c1", "q", timeout_s=5)
    assert [e["type"] for e in events] == ["text", "done"], "reading stops at done"


def test_wait_for_model_ready_requires_consecutive_ready_polls() -> None:
    """llama-server reports ready before it can serve; one ready poll is not enough."""
    sequence = iter(
        [
            {"state": "ready", "model_id": "m"},
            {"state": "loading", "model_id": "m"},
            {"state": "ready", "model_id": "m"},
            {"state": "ready", "model_id": "m"},
            {"state": "ready", "model_id": "m"},
        ]
    )
    seen: list[str] = []

    class Client(HarborClerk):
        def model_status(self) -> dict[str, Any]:
            status = next(sequence)
            seen.append(status["state"])
            return status

    with Client("http://localhost:1") as client:
        client.wait_for_model_ready("m", timeout_s=5, consecutive=3, poll_s=0.01)
    assert seen == ["ready", "loading", "ready", "ready", "ready"], "the streak must restart after a non-ready poll"


def test_wait_for_model_ready_times_out_with_the_last_status() -> None:
    class Client(HarborClerk):
        def model_status(self) -> dict[str, Any]:
            return {"state": "loading", "model_id": "m"}

    with Client("http://localhost:1") as client, pytest.raises(TimeoutError, match="loading"):
        client.wait_for_model_ready("m", timeout_s=0.05, poll_s=0.01)


def test_stream_ask_raises_on_an_http_error_with_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "No model is active"})

    with (
        HarborClerk("http://test", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError, match="No model"),
    ):
        client.stream_ask("c1", "q", timeout_s=5)
