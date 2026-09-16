"""Session fixtures for the live acceptance checks.

One folder of rendered fixtures is added to the instance under a per-run
name, ingested, and deleted at the end. Deleting the watched folder cascades
to its documents, so folder-scoped runs leave everything else on the
instance untouched. Every API key the run creates is deleted too. Wipe mode
is opt-in and double-guarded (see `config.py`).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from acceptance.config import WIPE_MAX_DOCUMENTS, AcceptanceConfig, load_config
from acceptance.fixtures.render import Fixture, load_groundtruth, materialize
from acceptance.hc_client import HarborClerk, McpSession


@pytest.fixture(scope="session")
def cfg() -> AcceptanceConfig:
    return load_config()


@pytest.fixture(scope="session")
def admin(cfg: AcceptanceConfig) -> Iterator[HarborClerk]:
    client = HarborClerk(cfg.api_base, verify=not cfg.insecure)
    health = client.health()
    if health.get("status") != "healthy":
        pytest.fail(f"instance at {cfg.api_base} is not healthy: {health}")
    client.login(cfg.username, cfg.password)
    if not client.user or client.user.get("role") != "admin":
        pytest.fail(f"{cfg.username} is not an admin; the suite creates folders and keys")
    yield client
    client.close()


@dataclass
class Corpus:
    folder_id: str
    folder_path: str
    fixtures: dict[str, Fixture]
    groundtruth: dict[str, Any]
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)  # fixture name -> DocumentSummary
    source_digests: dict[str, str] = field(default_factory=dict)  # fixture name -> sha256 at render time

    @property
    def scope(self) -> dict[str, list[str]]:
        return {"folder_ids": [self.folder_id]}

    def doc_id(self, fixture_name: str) -> str:
        return self.docs[fixture_name]["doc_id"]

    @property
    def doc_ids(self) -> set[str]:
        return {d["doc_id"] for d in self.docs.values()}

    def name_of(self, doc_id: str) -> str | None:
        return next((name for name, d in self.docs.items() if d["doc_id"] == doc_id), None)


def _wipe_instance(admin: HarborClerk, cfg: AcceptanceConfig) -> None:
    """Only on a disposable instance holding little: both guards, every time."""
    if not cfg.disposable:
        pytest.fail("wipe mode without HC_ACCEPTANCE_DISPOSABLE=1")
    count = admin.document_count()
    if count > WIPE_MAX_DOCUMENTS:
        pytest.fail(f"refusing to wipe {cfg.api_base}: it holds {count} documents (cap {WIPE_MAX_DOCUMENTS})")
    for folder in admin.folder_list():
        admin.folder_delete(folder["folder_id"])
    admin.request(
        "POST", "/api/system/delete-all-documents", json={"confirmation": "DELETE EVERYTHING"}
    ).raise_for_status()


def _documents_under(admin: HarborClerk, folder_path: str) -> dict[str, dict[str, Any]]:
    """Map fixture file name -> document row for documents from our folder."""
    rows = admin.list_documents(limit=200)["items"]
    out: dict[str, dict[str, Any]] = {}
    prefix = folder_path.rstrip("/") + "/"
    for row in rows:
        src = row.get("watch_source_path") or row.get("source_path") or ""
        if src.startswith(prefix):
            out[src[len(prefix) :]] = row
    return out


@pytest.fixture(scope="session")
def corpus(cfg: AcceptanceConfig, admin: HarborClerk) -> Iterator[Corpus]:
    if cfg.wipe:
        _wipe_instance(admin, cfg)

    # Register the empty folder first, then render into it. On Compose the
    # watcher auto-discovers top-level subdirectories of WATCH_ROOT every
    # minute; populating first can lose the race and leave an auto-mount that
    # refuses deletion. It also exercises the live detection path the product
    # claims, rather than only the initial scan.
    cfg.folder_path.mkdir(parents=True, exist_ok=False)
    folder = admin.folder_create(cfg.folder_path_in_instance)
    fixtures = {f.name: f for f in materialize(cfg.folder_path)}
    expected_files = sum(1 for f in fixtures.values() if not f.unsupported)
    corpus = Corpus(
        folder_id=folder["folder_id"],
        folder_path=folder["path"],
        fixtures=fixtures,
        groundtruth=load_groundtruth(),
        source_digests={f.name: hashlib.sha256(f.path.read_bytes()).hexdigest() for f in fixtures.values()},
    )
    keep = os.environ.get("HC_ACCEPTANCE_KEEP", "") == "1"
    try:
        try:
            admin.wait_for_folder_ingest(
                corpus.folder_id, expected_files=expected_files, timeout_s=cfg.ingest_timeout_s
            )
        except TimeoutError as e:
            summary = admin.status_summary()
            pytest.fail(
                f"{e}\nstatus-summary: {summary.get('state')} {summary.get('counts')}\n{summary.get('needs_attention')}"
            )
        corpus.docs = _documents_under(admin, corpus.folder_path)
        yield corpus
    finally:
        if not keep:
            admin.folder_delete(corpus.folder_id)
            shutil.rmtree(cfg.folder_path, ignore_errors=True)


class KeyFactory:
    """Creates API keys for a run and deletes every one of them afterwards."""

    def __init__(self, admin: HarborClerk, run_id: str):
        self._admin = admin
        self._run_id = run_id
        self.created: list[dict[str, Any]] = []

    def create(self, name: str, *, permission_tier: str = "full", **fields: Any) -> dict[str, Any]:
        key = self._admin.create_api_key(f"acceptance-{self._run_id}-{name}", permission_tier=permission_tier, **fields)
        self.created.append(key)
        return key

    def cleanup(self) -> None:
        for key in self.created:
            self._admin.request("DELETE", f"/api/api-keys/{key['key_id']}")


@pytest.fixture(scope="session")
def keys(cfg: AcceptanceConfig, admin: HarborClerk, corpus: Corpus) -> Iterator[KeyFactory]:
    factory = KeyFactory(admin, cfg.run_id)
    yield factory
    factory.cleanup()


@pytest.fixture(scope="session")
def mcp(cfg: AcceptanceConfig):
    """Factory for MCP sessions against this instance: `mcp.bearer(raw)` / `mcp.url_token(raw)`."""

    class _Factory:
        @staticmethod
        def bearer(raw_key: str) -> McpSession:
            return McpSession.bearer(cfg.api_base, raw_key)

        @staticmethod
        def url_token(raw_key: str) -> McpSession:
            return McpSession.url_token(cfg.api_base, raw_key)

    return _Factory()
