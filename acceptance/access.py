"""Helpers for the access-surface checks (MCP tiers, CLI parity, key limits).

Plain functions and context managers so they can be tested offline; the
fixtures in `conftest.py` only bind them to the session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]

# The tier contract, spelled out rather than imported so a change to the
# product's table (src/harbor_clerk/api/scope.py) is a failing check, not a
# moving target; an offline test compares these to the source.
SEARCH_TOOLS = frozenset(
    {"kb_search", "kb_batch_search", "kb_corpus_overview", "kb_list_recent", "kb_find_all", "kb_documents_by_date"}
)
READ_TOOLS = SEARCH_TOOLS | {
    "kb_read_passages",
    "kb_expand_context",
    "kb_document_outline",
    "kb_get_document",
    "kb_verify_identifier",
}
FULL_TOOLS = READ_TOOLS | {
    "kb_read_document",
    "kb_find_related",
    "kb_entity_search",
    "kb_entity_overview",
    "kb_entity_cooccurrence",
    "kb_ingest_status",
}
ADMIN_ONLY_TOOLS = frozenset({"kb_system_health", "kb_reprocess"})
TIER_TOOLS = {"search": SEARCH_TOOLS, "read": READ_TOOLS, "full": FULL_TOOLS}

# harbor-clerk CLI exit codes (src/harbor_clerk/cli/errors.py)
EXIT_OK, EXIT_USAGE, EXIT_CONNECTION, EXIT_CLI_DISABLED, EXIT_AUTH, EXIT_HTTP = 0, 1, 2, 3, 4, 5


# ── CLI ─────────────────────────────────────────────────────────────────────


def cli_env(api_base: str, raw_key: str, *, insecure: bool = False) -> dict[str, str]:
    """The environment the harbor-clerk CLI reads (src/harbor_clerk/cli/config.py)."""
    # HARBOR_CLERK_*: stale values from the shell would override ours. HC_*: the
    # suite's own settings, including the admin password, have no business in
    # a CLI subprocess. VIRTUAL_ENV from a foreign venv makes `uv run` print a
    # warning on stderr, where the CLI's structured error also goes.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HARBOR_CLERK_", "HC_")) and k != "VIRTUAL_ENV"}
    env["HARBOR_CLERK_URL"] = api_base
    env["HARBOR_CLERK_API_KEY"] = raw_key
    if insecure:
        env["HARBOR_CLERK_INSECURE_SKIP_VERIFY"] = "1"
    return env


@dataclass
class CliResult:
    code: int
    stdout: str
    stderr: str

    @property
    def json(self) -> Any:
        """The command's result payload (stdout)."""
        return json.loads(self.stdout)

    @property
    def error(self) -> Any:
        """The structured error the CLI writes to stderr on failure
        (cli/errors.py write_error). Anything before the first `{` is tooling
        noise (a `uv` warning, say), not the error."""
        start = self.stderr.find("{")
        if start < 0:
            raise ValueError(f"no JSON error on stderr: {self.stderr[:300]!r}")
        payload, _ = json.JSONDecoder().raw_decode(self.stderr[start:])
        return payload


def cli_command(*args: str) -> list[str]:
    """`harbor-clerk <args> --json` from this checkout's environment, not the
    instance's installed shim: parity is checked against the CLI in the tree.
    `--json` goes after the subcommand: the CLI's global flags are per-command."""
    return ["uv", "run", "--frozen", "--no-sync", "harbor-clerk", *args, "--json"]


def run_cli(api_base: str, raw_key: str, *args: str, insecure: bool = False, timeout_s: float = 120) -> CliResult:
    proc = subprocess.run(
        cli_command(*args),
        cwd=REPO,
        env=cli_env(api_base, raw_key, insecure=insecure),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return CliResult(proc.returncode, proc.stdout, proc.stderr)


# ── CLI access toggle ───────────────────────────────────────────────────────


def _read_config(config_json: Path) -> dict[str, Any]:
    """The file as a dict; anything else (empty, non-object) reads as {} the
    way the product's own reader treats it."""
    raw = config_json.read_bytes()
    data = json.loads(raw) if raw.strip() else {}
    return data if isinstance(data, dict) else {}


def _read_config_or(config_json: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    """Like `_read_config`, but a missing or unparseable file yields `fallback`
    instead of raising: used at restore time, where raising would skip the
    restore write and mask the check's own failure."""
    try:
        return _read_config(config_json)
    except (OSError, ValueError):
        return dict(fallback)


def _write_config_atomically(config_json: Path, data: dict[str, Any]) -> None:
    """Temp file + fsync + rename, the codebase's own idiom: the API re-reads
    this file per CLI request and the Swift app polls its mtime, so neither
    may see a truncated file. The temp file is created 0600 (the file holds
    `secret_key`) and then given the original's mode."""
    tmp = config_json.with_name(config_json.name + ".acceptance-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    if config_json.exists():
        shutil.copymode(config_json, tmp)
    os.replace(tmp, config_json)


@contextmanager
def cli_access_toggled(config_json: Path, enabled: bool) -> Iterator[None]:
    """Flip `enable_cli_access` in the native app's config.json for the block.

    There is no API for it: the API re-reads this file when a CLI request
    reaches the MCP auth middleware (config.refresh_cli_access_setting). Only
    that one key is touched, and it is restored in a finally after re-reading
    the file, so anything the app or the API wrote to other keys meanwhile
    survives. Formatting is normalised to two-space JSON.

    The API applies only the keys *present* in the file (config._apply_to), so
    a key that was absent is restored as an explicit `false`, the setting's
    default, never by removal: removing it would leave the running API on the
    value this block set, with the file and the Preferences toggle saying
    otherwise. Whether the gate actually followed is the caller's to check
    with a CLI request; `CliAccess.ensured` does."""
    before = _read_config(config_json)
    original = before.get("enable_cli_access", False)
    _write_config_atomically(config_json, {**before, "enable_cli_access": enabled})
    try:
        yield
    finally:
        # Re-read so other writers' changes survive; if the file is unreadable
        # right now, restore from what it was rather than skip the restore.
        current = _read_config_or(config_json, before)
        current["enable_cli_access"] = original
        _write_config_atomically(config_json, current)


# ── a second, empty watched folder (for scope checks) ───────────────────────


@contextmanager
def empty_folder_session(admin: Any, folder_path: Path, path_in_instance: str) -> Iterator[dict[str, Any]]:
    """Register an empty folder and remove it afterwards, whatever happens."""
    folder_path.mkdir(parents=True, exist_ok=False)
    folder_id: str | None = None
    try:
        folder = admin.folder_create(path_in_instance)
        folder_id = folder["folder_id"]
        yield folder
    finally:
        try:
            if folder_id is not None:
                admin.folder_delete(folder_id)
        finally:
            shutil.rmtree(folder_path, ignore_errors=True)


# ── a second, populated watched folder (documents outside the fixture scope) ─


@dataclass
class PopulatedFolder:
    folder_id: str
    path: str
    doc_id: str
    phrase: str  # a phrase found only in this folder's document


SECOND_FOLDER_SOURCE = "second-folder-note.txt"
SECOND_FOLDER_PHRASE = "cormorant ledger"


def choose_model(status: dict[str, Any], models: list[dict[str, Any]], *, allow_swap: bool) -> tuple[str, str]:
    """What the Ask checks should do about the model, from `models/status` and
    `models`. Returns (action, detail):

    - ("use", id): a model is active and ready; use it as found.
    - ("wait", id): a model is configured but llama-server is still loading it
      (just restarted, weights loading); wait for it rather than swap.
    - ("activate", id): nothing is configured; the smallest downloaded model may
      be activated because the run is allowed to change the instance. It stays
      active: there was nothing to restore, and deactivating is never called.
    - ("skip", reason): nothing downloaded, or a swap is not allowed."""
    state, model_id = status.get("state"), status.get("model_id")
    if model_id and state == "ready":
        return "use", model_id
    if model_id and state == "loading":
        return "wait", model_id
    downloaded = [m for m in models if m.get("downloaded")]
    if not downloaded:
        return "skip", "no local model is downloaded on this instance; the Ask checks need one"
    if not allow_swap:
        return "skip", (
            "no model is active; activating one changes the instance, allowed only with "
            "HC_ACCEPTANCE_DISPOSABLE=1 or HC_ACCEPTANCE_ALLOW_MODEL_SWAP=1"
        )
    return "activate", min(downloaded, key=lambda m: m["size_bytes"])["id"]


@contextmanager
def populated_folder_session(
    admin: Any,
    folder_path: Path,
    path_in_instance: str,
    *,
    source_text: str,
    timeout_s: float,
    documents_under: Any,
) -> Iterator[PopulatedFolder]:
    """Render one document into a new folder, register it, wait for it to be
    ready, yield; then remove the folder (cascading the document) and the
    files, tolerating a folder a check already deleted (H2)."""
    folder_path.mkdir(parents=True, exist_ok=False)
    folder_id: str | None = None
    try:
        (folder_path / SECOND_FOLDER_SOURCE).write_text(source_text, encoding="utf-8")
        folder = admin.folder_create(path_in_instance)
        folder_id = folder["folder_id"]
        admin.wait_for_folder_ingest(folder_id, expected_files=1, timeout_s=timeout_s)
        docs = documents_under(folder["path"], {SECOND_FOLDER_SOURCE})
        yield PopulatedFolder(folder_id, folder["path"], docs[SECOND_FOLDER_SOURCE]["doc_id"], SECOND_FOLDER_PHRASE)
    finally:
        try:
            if folder_id is not None:
                try:
                    admin.folder_delete(folder_id)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 404:
                        raise
        finally:
            shutil.rmtree(folder_path, ignore_errors=True)


# ── raw MCP probe ───────────────────────────────────────────────────────────

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "acceptance", "version": "0"},
    },
}


def mcp_probe(
    api_base: str,
    raw_key: str,
    *,
    url_token: bool = False,
    verify: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """HTTP status of an MCP `initialize` with this key on one of the two auth
    surfaces. 200 means the key was accepted; 401 means refused. Used where
    the outcome is the status itself (expired, deleted) and a client session
    would only wrap it in an exception."""
    base = api_base.rstrip("/")
    url = f"{base}/t/{raw_key}" if url_token else f"{base}/mcp/"
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if not url_token:
        headers["Authorization"] = f"Bearer {raw_key}"
    with httpx.Client(verify=verify, timeout=30, follow_redirects=False, transport=transport) as http:
        return http.post(url, json=_INITIALIZE, headers=headers).status_code
