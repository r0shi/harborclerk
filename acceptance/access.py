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

# The documented tier contract (docs/architecture.md, generated from
# api/scope.py). Spelled out here rather than imported so a change to the
# product's table is a failing check, not a moving target.
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
    # VIRTUAL_ENV from an activated foreign venv makes `uv run` print a warning
    # on stderr, where the CLI's structured error also goes.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARBOR_CLERK_") and k != "VIRTUAL_ENV"}
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
        return json.loads(self.stderr[start:])


def run_cli(api_base: str, raw_key: str, *args: str, insecure: bool = False, timeout_s: float = 120) -> CliResult:
    """Run `harbor-clerk <args> --json` from the repository's own environment.
    `--json` goes after the subcommand: the CLI's global flags are per-command."""
    proc = subprocess.run(
        ["uv", "run", "--frozen", "--no-sync", "harbor-clerk", *args, "--json"],
        cwd=REPO,
        env=cli_env(api_base, raw_key, insecure=insecure),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return CliResult(proc.returncode, proc.stdout, proc.stderr)


# ── CLI access toggle ───────────────────────────────────────────────────────


def _read_config(config_json: Path) -> dict[str, Any]:
    raw = config_json.read_bytes()
    return json.loads(raw) if raw.strip() else {}


def _write_config_atomically(config_json: Path, data: dict[str, Any]) -> None:
    """Temp file + rename, the codebase's own idiom: the API re-reads this
    file per CLI request and the Swift app polls its mtime, so neither may see
    a truncated file."""
    tmp = config_json.with_name(config_json.name + ".acceptance-tmp")
    tmp.write_bytes(json.dumps(data, indent=2).encode("utf-8") + b"\n")
    os.replace(tmp, config_json)


@contextmanager
def cli_access_toggled(config_json: Path, enabled: bool) -> Iterator[None]:
    """Flip `enable_cli_access` in the native app's config.json for the block.

    There is no API for it: the API re-reads this file when a CLI request
    reaches the MCP auth middleware (config.refresh_cli_access_setting). Only
    that one key is touched, and it is restored from its original value in a
    finally after re-reading the file, so a check that fails cannot leave the
    operator's setting flipped and anything the app or the API wrote to other
    keys meanwhile survives. Formatting is normalised to two-space JSON."""
    before = _read_config(config_json)
    had_key = "enable_cli_access" in before
    original = before.get("enable_cli_access")
    _write_config_atomically(config_json, {**before, "enable_cli_access": enabled})
    try:
        yield
    finally:
        current = _read_config(config_json)
        if had_key:
            current["enable_cli_access"] = original
        else:
            current.pop("enable_cli_access", None)
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


def mcp_probe(api_base: str, raw_key: str, *, url_token: bool = False, verify: bool = True) -> int:
    """HTTP status of an MCP `initialize` with this key on one of the two auth
    surfaces. 200 means the key was accepted; 401 means refused. Used where
    the outcome is the status itself (expired, deleted) and a client session
    would only wrap it in an exception."""
    base = api_base.rstrip("/")
    url = f"{base}/t/{raw_key}" if url_token else f"{base}/mcp/"
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if not url_token:
        headers["Authorization"] = f"Bearer {raw_key}"
    with httpx.Client(verify=verify, timeout=30, follow_redirects=False) as http:
        return http.post(url, json=_INITIALIZE, headers=headers).status_code
