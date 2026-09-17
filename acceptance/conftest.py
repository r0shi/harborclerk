"""Session fixtures for the live acceptance checks.

One folder of rendered fixtures is added to the instance under a per-run
name, ingested, and deleted at the end. Deleting the watched folder cascades
to its documents, so folder-scoped runs leave every other document on the
instance untouched. What a run does leave: audit rows (login, key create and
delete, reprocess, one document soft-deleted, conversations created and
deleted), soft-deleted keys, three extra watched folders registered and
removed (one empty, one with a single document, one for H2), and, when
`HC_ACCEPTANCE_CONFIG_JSON` is set, a rewritten config.json with the same
settings in two-space JSON and `enable_cli_access` spelled out. If no model
was active and the run was allowed to activate one, that model stays active
and becomes the app's persisted default (activation writes `llm_model_id` to
config.json). `HC_ACCEPTANCE_KEEP=1` keeps only the fixture folder. Wipe mode
is opt-in and double-guarded (see `config.py`).

The setup and teardown are plain functions so they can be tested offline
with a fake client; the pytest fixtures only bind them to the session.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
import pytest

from acceptance.access import (
    EXIT_CLI_DISABLED,
    EXIT_OK,
    PopulatedFolder,
    cli_access_toggled,
    empty_folder_session,
    populated_folder_session,
    run_cli,
)
from acceptance.config import WIPE_MAX_DOCUMENTS, AcceptanceConfig, load_config
from acceptance.fixtures.render import Fixture, load_groundtruth, materialize, source_text
from acceptance.hc_client import HarborClerk, McpSession


class AdminClient(Protocol):
    """The slice of `HarborClerk` the session helpers use; a fake implements it offline."""

    def document_count(self) -> int: ...
    def folder_list(self) -> list[dict[str, Any]]: ...
    def folder_create(self, path: str, *, recursive: bool = True) -> dict[str, Any]: ...
    def folder_delete(self, folder_id: str) -> None: ...
    def wait_for_folder_ingest(
        self, folder_id: str, *, expected_files: int, timeout_s: float, poll_s: float = 3.0
    ) -> dict[str, Any]: ...
    def list_documents(self, **params: Any) -> dict[str, Any]: ...
    def status_summary(self) -> dict[str, Any]: ...
    def watch_system(self) -> dict[str, Any]: ...
    def request(self, method: str, path: str, **kw: Any) -> httpx.Response: ...


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
    if client.watch_system().get("platform") == "docker":
        pytest.skip(
            "Compose targets are not supported yet (watcher auto-discovery creates a twin folder); see the spec"
        )
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
    deleted_chunk_id: str | None = None  # set by H1 for H1c

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


def wipe_instance(admin: AdminClient, cfg: AcceptanceConfig) -> None:
    """Only on a disposable instance holding little: both guards, every time.
    Every watched folder must go first; if any refuses, stop before touching
    documents rather than leave a half-wiped instance."""
    if not cfg.disposable:
        pytest.fail("wipe mode without HC_ACCEPTANCE_DISPOSABLE=1")
    count = admin.document_count()
    if count > WIPE_MAX_DOCUMENTS:
        pytest.fail(f"refusing to wipe {cfg.api_base}: it holds {count} documents (cap {WIPE_MAX_DOCUMENTS})")
    refused: list[str] = []
    for folder in admin.folder_list():
        try:
            admin.folder_delete(folder["folder_id"])
        except httpx.HTTPStatusError as e:
            refused.append(f"{folder.get('path')}: {e.response.status_code}")
    if refused:
        pytest.fail(f"refusing to wipe: these folders could not be deleted, documents left intact: {refused}")
    admin.request(
        "POST", "/api/system/delete-all-documents", json={"confirmation": "DELETE EVERYTHING"}
    ).raise_for_status()


def documents_under(
    admin: AdminClient, folder_path: str, *, expected: set[str] | None = None, page: int = 200, max_pages: int = 25
) -> dict[str, dict[str, Any]]:
    """Map fixture file name -> document row for documents from our folder.

    `GET /api/docs` has no folder filter, so this pages the list, most recently
    updated first, until every expected name is found or the list ends. The
    fixtures are the most recently finalized documents right after ingest, so
    the first page normally suffices; the paging is for instances busy with
    other work."""
    prefix = folder_path.rstrip("/") + "/"
    out: dict[str, dict[str, Any]] = {}
    for n in range(max_pages):
        items = admin.list_documents(limit=page, offset=n * page)["items"]
        for row in items:
            src = row.get("watch_source_path") or row.get("source_path") or ""
            if src.startswith(prefix):
                out[src[len(prefix) :]] = row
        if len(items) < page or (expected is not None and expected <= set(out)):
            break
    return out


@contextmanager
def corpus_session(admin: AdminClient, cfg: AcceptanceConfig, *, keep: bool = False) -> Iterator[Corpus]:
    """Render, register, ingest, yield; then remove the folder and the files.

    Files are rendered *before* the folder is registered so the watcher's
    initial scan sees all of them: `skipped_extensions` is written only by
    that scan, and live events for unsupported files are dropped without
    updating the tally. The cleanup runs whatever failed, including a failed
    ingest wait, so a run never leaves a registered folder behind."""
    if cfg.wipe:
        wipe_instance(admin, cfg)
    cfg.folder_path.mkdir(parents=True, exist_ok=False)
    folder_id: str | None = None
    try:
        fixtures = {f.name: f for f in materialize(cfg.folder_path)}
        digests = {f.name: hashlib.sha256(f.path.read_bytes()).hexdigest() for f in fixtures.values()}
        expected_names = {name for name, f in fixtures.items() if not f.unsupported}
        expected_files = len(expected_names)
        folder = admin.folder_create(cfg.folder_path_in_instance)
        folder_id = folder["folder_id"]
        corpus = Corpus(
            folder_id=folder_id,
            folder_path=folder["path"],
            fixtures=fixtures,
            groundtruth=load_groundtruth(),
            source_digests=digests,
        )
        try:
            admin.wait_for_folder_ingest(folder_id, expected_files=expected_files, timeout_s=cfg.ingest_timeout_s)
        except (TimeoutError, RuntimeError) as e:
            summary = admin.status_summary()
            pytest.fail(
                f"{e}\nstatus-summary: {summary.get('state')} {summary.get('counts')}\n{summary.get('needs_attention')}"
            )
        corpus.docs = documents_under(admin, corpus.folder_path, expected=expected_names)
        yield corpus
    finally:
        if not keep:
            try:
                if folder_id is not None:
                    admin.folder_delete(folder_id)
            finally:
                # The files go even if the folder delete raised (401 after a
                # long run, 5xx): a registered folder pointing at nothing is
                # easier to spot and remove than one that keeps ingesting.
                shutil.rmtree(cfg.folder_path, ignore_errors=True)


@pytest.fixture(scope="session")
def corpus(cfg: AcceptanceConfig, admin: HarborClerk) -> Iterator[Corpus]:
    with corpus_session(admin, cfg, keep=cfg.keep) as c:
        yield c


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

    def cleanup(self) -> list[str]:
        """Delete every key created; return the ids that could not be deleted."""
        failed: list[str] = []
        for key in self.created:
            if self._admin.request("DELETE", f"/api/api-keys/{key['key_id']}").status_code >= 400:
                failed.append(key["key_id"])
        return failed


@pytest.fixture(scope="session")
def keys(cfg: AcceptanceConfig, admin: HarborClerk, corpus: Corpus) -> Iterator[KeyFactory]:
    factory = KeyFactory(admin, cfg.run_id)
    yield factory
    failed = factory.cleanup()
    if failed:
        pytest.fail(f"API keys created by this run could not be deleted and are still active: {failed}")


@pytest.fixture(scope="session")
def empty_folder(cfg: AcceptanceConfig, admin: HarborClerk, corpus: Corpus) -> Iterator[dict[str, Any]]:
    """A second, registered, empty watched folder: a key scoped to it can see
    none of the fixtures (G3)."""
    name = f"{cfg.folder_name}-empty"
    with empty_folder_session(admin, cfg.folder_root / name, str(Path(cfg.folder_path_in_instance).parent / name)) as f:
        yield f


@pytest.fixture(scope="session")
def second_folder(cfg: AcceptanceConfig, admin: HarborClerk, corpus: Corpus) -> Iterator[PopulatedFolder]:
    """A second watched folder holding one document the fixture-scoped keys
    must not see (G8, G9)."""
    name = f"{cfg.folder_name}-second"
    with populated_folder_session(
        admin,
        cfg.folder_root / name,
        str(Path(cfg.folder_path_in_instance).parent / name),
        source_text=source_text("second-folder-note.txt"),
        timeout_s=cfg.ingest_timeout_s,
        documents_under=lambda path, expected: documents_under(admin, path, expected=expected),
    ) as f:
        yield f


class CliAccess:
    """What the instance's CLI gate currently is, judged by a real CLI request
    (the health endpoint reports the setting only as of the last CLI request
    the API saw), and a way to flip it when the operator has pointed the suite
    at the native config.json. `runner` is `run_cli` in production and a fake
    in the offline tests."""

    def __init__(self, cfg: AcceptanceConfig, raw_key: str, runner=run_cli, sleeper=time.sleep):
        self._cfg = cfg
        self._raw_key = raw_key
        self._runner = runner
        self._sleeper = sleeper
        self.flipped = False

    def probe(self) -> int:
        """Exit code of a minimal CLI search; also makes the API re-read the gate."""
        return self._runner(
            self._cfg.api_base, self._raw_key, "search", "probe", "-k", "1", insecure=self._cfg.insecure
        ).code

    @property
    def enabled(self) -> bool:
        code = self.probe()
        if code == EXIT_OK:
            return True
        if code == EXIT_CLI_DISABLED:
            return False
        raise RuntimeError(f"CLI probe exited {code}; cannot tell whether CLI access is enabled")

    def toggled(self, enabled: bool):
        if self._cfg.config_json is None:
            pytest.skip(
                "flipping CLI access needs HC_ACCEPTANCE_CONFIG_JSON pointing at the native config.json on this host"
            )
        self.flipped = True
        return cli_access_toggled(self._cfg.config_json, enabled)

    def verify_settled(self, expected_enabled: bool, *, settle_s: float = 3.5) -> None:
        """Closes the race with the Swift app: it caches config.json on a 3 s
        poll and writes the whole dict on any save, so a save landing within
        3 s of a restore can write the flipped value back. If this session
        flipped the gate, wait past that window and check the gate once more;
        a mismatch fails the run rather than leaving the instance changed."""
        if not self.flipped:
            return
        self._sleeper(settle_s)
        if self.enabled != expected_enabled:
            pytest.fail(
                f"CLI access is {'enabled' if not expected_enabled else 'disabled'} after the run but was "
                f"{'disabled' if not expected_enabled else 'enabled'} before it; a writer restored a flipped value"
            )

    @contextmanager
    def ensured(self) -> Iterator[None]:
        """CLI access on for the block: as found, or flipped and then restored.

        The probe after the restore is load-bearing: it makes the API re-read
        the file and proves the gate went back to disabled. If it did not, the
        run fails here rather than leaving the instance in a state the file
        does not describe."""
        if self.enabled:
            yield
            return
        if self._cfg.config_json is None:
            pytest.skip("CLI access is disabled on this instance and HC_ACCEPTANCE_CONFIG_JSON is not set")
        try:
            with self.toggled(True):
                yield
        finally:
            code = self.probe()
            if code != EXIT_CLI_DISABLED:
                pytest.fail(
                    f"CLI access did not return to disabled after the restore (probe exit {code}); "
                    "the API's in-memory gate no longer matches config.json"
                )


@pytest.fixture(scope="session")
def cli_access(cfg: AcceptanceConfig, keys: KeyFactory, corpus: Corpus) -> Iterator[CliAccess]:
    access = CliAccess(cfg, keys.create("cli-probe", scope_folder_ids=[corpus.folder_id])["raw_key"])
    initial = access.enabled
    yield access
    access.verify_settled(initial)


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
