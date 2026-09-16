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
from acceptance.hc_client import HarborClerk, document_ready, folder_ingest_done


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
    ):
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
        return {"items": self.items, "total": len(self.items)}

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


def test_documents_under_maps_by_relative_path(tmp_path: Path) -> None:
    folder = tmp_path / "f"
    admin = FakeAdmin(items=_items_for(folder) + [{"doc_id": "d-src", "source_path": str(folder / "x.txt")}])
    mapped = conftest.documents_under(admin, str(folder))
    assert mapped == {"lease-agreement.pdf": admin.items[0], "x.txt": admin.items[2]}


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


def test_wait_for_document_ready_detects_the_error_status_immediately() -> None:
    class Client(HarborClerk):
        def get_document(self, doc_id: str) -> dict[str, Any]:
            return {"doc_id": doc_id, "pipeline_status": "error", "error": "boom", "jobs": []}

    with Client("http://localhost:1") as client, pytest.raises(RuntimeError, match="boom"):
        client.wait_for_document_ready("d1", timeout_s=2, poll_s=0.1)
