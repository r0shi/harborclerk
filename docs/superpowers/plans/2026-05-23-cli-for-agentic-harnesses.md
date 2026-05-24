# `harbor-clerk` CLI for agentic harnesses — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `harbor-clerk` CLI that is an MCP-over-HTTP client to the local daemon, gated by a server-side toggle, audit-logged distinctly, with comprehensive `--help` per subcommand and a copy-pasteable Skill stub exposed in the Integrations page.

**Architecture:** The CLI speaks JSON-RPC to the existing `POST /mcp` endpoint, identifying itself by `User-Agent: harbor-clerk-cli/<version>`. The MCP auth middleware sniffs the UA and (a) rejects with HTTP 403 when `enable_cli_access=False`, (b) tags audit rows as `request_type="cli_tool"`. The CLI binary is a Python entry point shipped with the existing `harbor_clerk` package; the macOS app installs a `/usr/local/bin/harbor-clerk` shim.

**Tech Stack:** Python 3.12 (argparse, httpx, asyncio), Pydantic Settings, FastAPI/Starlette ASGI middleware, SQLAlchemy 2.0 async, pytest, React 19/TypeScript (Integrations page), Swift (macOS shim installer).

---

## File Structure

### Backend — server gate

- **Modify:** `src/harbor_clerk/config.py` — add `enable_cli_access: bool = False` field
- **Modify:** `src/harbor_clerk/mcp_server.py` — UA-sniff in `MCPAuthMiddleware` + UA-aware `request_type` in 4 audit-log sites
- **Modify:** `src/harbor_clerk/api/routes/system.py` — surface `enable_cli_access` in `/api/system/health`
- **Create:** `tests/api/test_cli_access_gate.py` — middleware gate tests
- **Modify:** `tests/mcp/test_mcp_audit.py` (or create if absent) — `request_type` distinction

### Backend — CLI

- **Create:** `src/harbor_clerk/cli/__init__.py` — package marker + version export
- **Create:** `src/harbor_clerk/cli/main.py` — entry point, top-level argparse, command dispatch
- **Create:** `src/harbor_clerk/cli/config.py` — env-var + flag resolution into a `CliConfig` dataclass
- **Create:** `src/harbor_clerk/cli/client.py` — `McpHttpClient` (HTTPX JSON-RPC wrapper)
- **Create:** `src/harbor_clerk/cli/output.py` — JSON / text rendering, TTY detection
- **Create:** `src/harbor_clerk/cli/errors.py` — exit-code constants + error → exit-code mapping
- **Create:** `src/harbor_clerk/cli/commands/__init__.py` — registers all subparsers
- **Create:** `src/harbor_clerk/cli/commands/<name>.py` (16 files) — one per tool
- **Create:** `src/harbor_clerk/cli/help/__init__.py` — loader for `.txt` files
- **Create:** `src/harbor_clerk/cli/help/<name>.txt` (16 files) — per-subcommand long help text
- **Modify:** `pyproject.toml` — new `[project.scripts]` entry `harbor-clerk = "harbor_clerk.cli.main:main"`

### Backend — CLI tests

- **Create:** `tests/cli/__init__.py`
- **Create:** `tests/cli/test_main.py` — top-level argparse, version, unknown command
- **Create:** `tests/cli/test_config.py` — env-var + flag resolution
- **Create:** `tests/cli/test_client.py` — JSON-RPC encoding, error mapping
- **Create:** `tests/cli/test_output.py` — JSON / text / TTY-detect logic
- **Create:** `tests/cli/test_errors.py` — exit-code mapping
- **Create:** `tests/cli/test_commands_search.py` — full per-command test as reference pattern
- **Create:** `tests/cli/test_commands_<name>.py` (15 more) — per-command tests
- **Create:** `tests/cli/test_e2e.py` — end-to-end against a running test daemon

### Frontend — Integrations page

- **Modify:** `frontend/src/hooks/useSystemConfig.ts` — read `enable_cli_access` and `cli_install_status`
- **Modify:** `frontend/src/pages/IntegrationsPage.tsx` — add "Agentic CLI" card
- **Create:** `frontend/src/components/CliAccessCard.tsx` — the card component (toggle status, install status, skill copy block)
- **Create:** `frontend/src/components/CliAccessCard.test.tsx` — component test

### macOS — shim installer

- **Modify:** `macos/HarborClerkServer/Sources/HarborClerkServer/CliShimInstaller.swift` (create) — `AuthorizationServices` shim install
- **Modify:** the Settings/preferences pane to surface install status and trigger reinstall
- **Modify:** the system-health endpoint passthrough so the frontend can read install state

### In-repo skill markdown

- **Create:** `skills/harbor-clerk/SKILL.md` — the skill markdown documented in the spec

### Docs

- **Modify:** `README.md` — short "Agentic CLI" section
- **Modify:** `CLAUDE.md` — add CLI to Key API Surface

---

## Phase 1: Server gate (3 tasks)

### Task 1: Add `enable_cli_access` setting

**Files:**
- Modify: `src/harbor_clerk/config.py` (around line 128 next to `allow_source_download`)
- Test: `tests/test_config.py` (create if absent, else extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
import os
from harbor_clerk.config import Settings


def test_enable_cli_access_defaults_false(monkeypatch):
    monkeypatch.delenv("ENABLE_CLI_ACCESS", raising=False)
    s = Settings()
    assert s.enable_cli_access is False


def test_enable_cli_access_reads_env(monkeypatch):
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
    s = Settings()
    assert s.enable_cli_access is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_enable_cli_access_defaults_false -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'enable_cli_access'`

- [ ] **Step 3: Add the field**

In `src/harbor_clerk/config.py`, add next to `allow_source_download`:

```python
    enable_cli_access: bool = Field(
        default=False,
        description=(
            "If true, the harbor-clerk CLI (User-Agent: harbor-clerk-cli/*) "
            "can call MCP tools. Default off; audit-logged as request_type=cli_tool."
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v -k cli_access`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/config.py tests/test_config.py
git commit -m "feat(config): add enable_cli_access setting (default off)"
```

---

### Task 2: UA-aware request typing + CLI access gate in MCP middleware

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py` (`MCPAuthMiddleware.__call__`, contextvars, 4 audit-log sites)
- Test: `tests/api/test_cli_access_gate.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/api/test_cli_access_gate.py`:

```python
import json
import pytest
from httpx import AsyncClient, ASGITransport

from harbor_clerk.api.app import create_app
from harbor_clerk.config import get_settings


@pytest.mark.asyncio
async def test_cli_request_rejected_when_disabled(api_key_factory, monkeypatch):
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
    get_settings.cache_clear()
    key = await api_key_factory()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {key.plaintext}",
                "User-Agent": "harbor-clerk-cli/0.1.0",
            },
        )
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "cli_access_disabled"
    assert "Integrations" in body["hint"]


@pytest.mark.asyncio
async def test_cli_request_allowed_when_enabled(api_key_factory, monkeypatch):
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
    get_settings.cache_clear()
    key = await api_key_factory()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {key.plaintext}",
                "User-Agent": "harbor-clerk-cli/0.1.0",
            },
        )
    assert resp.status_code == 200  # tools/list succeeds


@pytest.mark.asyncio
async def test_mcp_request_unaffected_by_cli_toggle(api_key_factory, monkeypatch):
    """Regular MCP clients (Claude.ai, etc.) must pass through regardless."""
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "false")
    get_settings.cache_clear()
    key = await api_key_factory()
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "Authorization": f"Bearer {key.plaintext}",
                "User-Agent": "Claude/1.0",
            },
        )
    assert resp.status_code == 200
```

Note: the `api_key_factory` fixture is assumed to exist in `tests/conftest.py`. If it does not, look for the equivalent (e.g. `make_api_key`, `factories.api_key`) and adapt the imports. **Do not create a new fixture if a working one exists.**

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_cli_access_gate.py -v`
Expected: All 3 fail. `test_cli_request_rejected_when_disabled` will likely return 200 (no gate yet); the other two should pass as side effects or fail because the assertion expects 403.

- [ ] **Step 3: Add UA detection + gate in `MCPAuthMiddleware`**

In `src/harbor_clerk/mcp_server.py`, add near the top of the file (next to `_mcp_principal`):

```python
_mcp_is_cli: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_mcp_is_cli",
    default=False,
)

_CLI_USER_AGENT_PREFIX = "harbor-clerk-cli/"


def _request_type_for_ua(scope) -> str:
    """Return 'cli_tool' if the request is from harbor-clerk-cli, else 'mcp_tool'."""
    return "cli_tool" if _mcp_is_cli.get() else "mcp_tool"
```

Then in `MCPAuthMiddleware.__call__`, after successful principal resolution and before forwarding to `self.app`, add:

```python
            if principal is not None:
                headers = dict(scope.get("headers", []))
                ua = headers.get(b"user-agent", b"").decode(errors="replace")
                is_cli = ua.startswith(_CLI_USER_AGENT_PREFIX)

                if is_cli:
                    from harbor_clerk.config import get_settings as _gs
                    if not _gs().enable_cli_access:
                        # Audit the rejection
                        try:
                            from harbor_clerk.api.request_log import log_api_request
                            async with async_session_factory() as log_session:
                                await log_api_request(
                                    log_session,
                                    api_key_id=principal.id if principal.type == "api_key" else None,
                                    request_type="cli_tool",
                                    endpoint="<gate>",
                                    parameters=None,
                                    status="denied",
                                    status_detail="cli_access_disabled",
                                    duration_ms=0,
                                )
                                await log_session.commit()
                        except Exception:
                            logger.debug("Failed to log CLI gate denial", exc_info=True)

                        body = json.dumps({
                            "error": "cli_access_disabled",
                            "hint": "Enable in System Settings → Integrations",
                        }).encode()
                        await send({
                            "type": "http.response.start",
                            "status": 403,
                            "headers": [
                                [b"content-type", b"application/json"],
                                [b"content-length", str(len(body)).encode()],
                            ],
                        })
                        await send({"type": "http.response.body", "body": body})
                        return

                reset_token = _mcp_principal.set(principal)
                cli_reset = _mcp_is_cli.set(is_cli)
                try:
                    await self.app(scope, receive, send)
                finally:
                    _mcp_is_cli.reset(cli_reset)
                    _mcp_principal.reset(reset_token)
                return
```

- [ ] **Step 4: Update the 4 audit-log sites in mcp_server.py to use UA-aware request_type**

Find every occurrence of `request_type="mcp_tool"` in `src/harbor_clerk/mcp_server.py` (there are 4 in the auth/audit path) and replace with `request_type=_request_type_for_ua(None)` (the function takes no real arg — it reads the contextvar).

Simplify the helper to take no argument:

```python
def _request_type_for_ua() -> str:
    return "cli_tool" if _mcp_is_cli.get() else "mcp_tool"
```

And use `request_type=_request_type_for_ua()` at each site.

- [ ] **Step 5: Run gate tests**

Run: `uv run pytest tests/api/test_cli_access_gate.py -v`
Expected: 3 passed

- [ ] **Step 6: Run the full MCP test suite to verify no regressions**

Run: `uv run pytest tests/mcp -v`
Expected: All existing tests still pass.

- [ ] **Step 7: Commit**

```bash
git add src/harbor_clerk/mcp_server.py tests/api/test_cli_access_gate.py
git commit -m "feat(mcp): UA-aware request typing + CLI access gate"
```

---

### Task 3: Surface `enable_cli_access` in `/api/system/health`

**Files:**
- Modify: `src/harbor_clerk/api/routes/system.py` (around line 104 where `allow_source_download` is exposed)
- Test: extend whichever test file covers the system route (search for `allow_source_download` in tests)

- [ ] **Step 1: Locate the test**

Run: `grep -rln "allow_source_download" tests/`
Expected: A test file using `allow_source_download` in an assertion against `/api/system/health`.

- [ ] **Step 2: Add a failing test mirroring the `allow_source_download` test**

In that test file, add:

```python
def test_health_endpoint_includes_enable_cli_access(client):
    resp = client.get("/api/system/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "enable_cli_access" in data
    assert data["enable_cli_access"] is False  # default
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest <that-test-file> -v -k cli_access`
Expected: FAIL — key missing from response.

- [ ] **Step 4: Add the field in the route handler**

In `src/harbor_clerk/api/routes/system.py`, find the dict returning `allow_source_download` and add:

```python
        "enable_cli_access": get_settings().enable_cli_access,
```

- [ ] **Step 5: Verify the test passes**

Run: `uv run pytest <that-test-file> -v -k cli_access`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/api/routes/system.py tests/<that-test-file>
git commit -m "feat(api): expose enable_cli_access in /api/system/health"
```

---

## Phase 2: CLI scaffold (5 tasks)

### Task 4: Create CLI package + entry point + top-level argparse

**Files:**
- Create: `src/harbor_clerk/cli/__init__.py`
- Create: `src/harbor_clerk/cli/main.py`
- Create: `tests/cli/__init__.py`
- Create: `tests/cli/test_main.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/__init__.py` (empty) and `tests/cli/test_main.py`:

```python
import subprocess
import sys


def run_cli(*args, env=None):
    """Invoke the CLI as a subprocess and return (stdout, stderr, returncode)."""
    result = subprocess.run(
        [sys.executable, "-m", "harbor_clerk.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout, result.stderr, result.returncode


def test_cli_version_flag():
    out, _err, rc = run_cli("--version")
    assert rc == 0
    assert "harbor-clerk-cli/" in out


def test_cli_help_flag_lists_all_16_commands():
    out, _err, rc = run_cli("--help")
    assert rc == 0
    for cmd in [
        "search", "batch-search", "read-passages", "expand-context", "read-document",
        "get-document", "list-recent", "corpus-overview", "document-outline", "find-related",
        "entity-search", "entity-overview", "entity-cooccurrence",
        "ingest-status", "reprocess", "system-health",
    ]:
        assert cmd in out, f"missing subcommand in --help: {cmd}"


def test_cli_unknown_command_exits_1():
    _out, err, rc = run_cli("totally-fake-subcommand")
    assert rc == 1
    assert "invalid choice" in err.lower() or "unknown" in err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_main.py -v`
Expected: All three fail with `ModuleNotFoundError: No module named 'harbor_clerk.cli.main'`.

- [ ] **Step 3: Create the CLI package**

Create `src/harbor_clerk/cli/__init__.py`:

```python
"""harbor-clerk CLI — MCP-over-HTTP client for agentic harnesses."""

__version__ = "0.1.0"
```

Create `src/harbor_clerk/cli/main.py`:

```python
"""harbor-clerk CLI entry point."""
from __future__ import annotations

import argparse
import sys

from harbor_clerk.cli import __version__
from harbor_clerk.cli.commands import register_all


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harbor-clerk",
        description=(
            "Query a Harbor Clerk knowledge base from the shell. "
            "Run `harbor-clerk <command> --help` for command details."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"harbor-clerk-cli/{__version__}",
    )
    parser.add_argument("--url", help="Override $HARBOR_CLERK_URL")
    parser.add_argument("--api-key", help="Override $HARBOR_CLERK_API_KEY")
    parser.add_argument("--insecure", action="store_true", help="Allow self-signed TLS")
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Force JSON output")
    output_group.add_argument("--format", choices=["text", "json"], help="Output format")

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    register_all(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return 1
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
```

Create `src/harbor_clerk/cli/commands/__init__.py` (placeholder — Task 9 will start populating it):

```python
"""Subcommand registrations for harbor-clerk CLI."""
from __future__ import annotations

import argparse

# Subcommand modules register themselves here via register_all().
_COMMAND_NAMES = [
    "search",
    "batch-search",
    "read-passages",
    "expand-context",
    "read-document",
    "get-document",
    "list-recent",
    "corpus-overview",
    "document-outline",
    "find-related",
    "entity-search",
    "entity-overview",
    "entity-cooccurrence",
    "ingest-status",
    "reprocess",
    "system-health",
]


def register_all(subparsers: argparse._SubParsersAction) -> None:
    """Register every subcommand parser. Imports done lazily to keep startup fast."""
    for name in _COMMAND_NAMES:
        module_name = name.replace("-", "_")
        try:
            module = __import__(
                f"harbor_clerk.cli.commands.{module_name}",
                fromlist=["add_parser"],
            )
        except ImportError:
            # Placeholder so --help can list the command before implementation lands.
            p = subparsers.add_parser(name, help=f"[stub] {name}")
            p.set_defaults(_handler=_stub_handler(name))
            continue
        module.add_parser(subparsers)


def _stub_handler(name):
    def _h(args):
        import sys
        print(f"harbor-clerk: {name} not yet implemented", file=sys.stderr)
        return 2
    return _h
```

Modify `pyproject.toml`, in `[project.scripts]`:

```toml
harbor-clerk = "harbor_clerk.cli.main:main"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_main.py -v`
Expected: All 3 pass. (The 16 commands appear in `--help` because the stubs register them even before real impl lands.)

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/cli/__init__.py src/harbor_clerk/cli/main.py \
        src/harbor_clerk/cli/commands/__init__.py \
        tests/cli/__init__.py tests/cli/test_main.py \
        pyproject.toml
git commit -m "feat(cli): scaffold harbor-clerk CLI entry point + subparser plumbing"
```

---

### Task 5: Config resolution (env vars + flags)

**Files:**
- Create: `src/harbor_clerk/cli/config.py`
- Create: `tests/cli/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_config.py`:

```python
import pytest

from harbor_clerk.cli.config import CliConfig, resolve_config


def test_url_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_URL", raising=False)
    cfg = resolve_config(url=None, api_key="hc_test", insecure=False)
    assert cfg.url == "https://localhost"


def test_url_from_env(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_URL", "https://example.test")
    cfg = resolve_config(url=None, api_key="hc_test", insecure=False)
    assert cfg.url == "https://example.test"


def test_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_URL", "https://env.test")
    cfg = resolve_config(url="https://flag.test", api_key="hc_test", insecure=False)
    assert cfg.url == "https://flag.test"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_API_KEY", raising=False)
    with pytest.raises(ValueError) as exc:
        resolve_config(url=None, api_key=None, insecure=False)
    assert "HARBOR_CLERK_API_KEY" in str(exc.value)


def test_insecure_from_env(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_INSECURE_SKIP_VERIFY", "true")
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", "hc_test")
    cfg = resolve_config(url=None, api_key=None, insecure=False)
    assert cfg.insecure is True


def test_insecure_flag(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_INSECURE_SKIP_VERIFY", raising=False)
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", "hc_test")
    cfg = resolve_config(url=None, api_key=None, insecure=True)
    assert cfg.insecure is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_config.py -v`
Expected: All fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the config resolver**

Create `src/harbor_clerk/cli/config.py`:

```python
"""Resolve CLI configuration from env vars and CLI flags."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CliConfig:
    url: str
    api_key: str
    insecure: bool


def resolve_config(
    *,
    url: str | None,
    api_key: str | None,
    insecure: bool,
) -> CliConfig:
    """Resolve config from flags first, then env vars, then defaults."""
    resolved_url = url or os.environ.get("HARBOR_CLERK_URL") or "https://localhost"
    resolved_api_key = api_key or os.environ.get("HARBOR_CLERK_API_KEY")
    if not resolved_api_key:
        raise ValueError(
            "Missing API key. Set HARBOR_CLERK_API_KEY or pass --api-key. "
            "Generate a key in System Settings → API Keys."
        )
    resolved_insecure = insecure or _truthy(os.environ.get("HARBOR_CLERK_INSECURE_SKIP_VERIFY"))
    return CliConfig(
        url=resolved_url.rstrip("/"),
        api_key=resolved_api_key,
        insecure=resolved_insecure,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_config.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/cli/config.py tests/cli/test_config.py
git commit -m "feat(cli): config resolution from env vars + flags"
```

---

### Task 6: MCP-over-HTTP client

**Files:**
- Create: `src/harbor_clerk/cli/client.py`
- Create: `tests/cli/test_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_client.py`:

```python
import json
import pytest
import respx
from httpx import Response

from harbor_clerk.cli import __version__
from harbor_clerk.cli.client import McpHttpClient, McpClientError
from harbor_clerk.cli.config import CliConfig


@pytest.fixture
def cfg():
    return CliConfig(url="https://test.local", api_key="hc_test", insecure=False)


@respx.mock
def test_call_tool_returns_parsed_json(cfg):
    respx.post("https://test.local/mcp").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps({"results": [{"score": 0.9}]})}
                    ]
                },
            },
        )
    )
    client = McpHttpClient(cfg)
    payload = client.call_tool("kb_search", {"query": "test"})
    assert payload == {"results": [{"score": 0.9}]}


@respx.mock
def test_call_tool_sends_user_agent_and_auth(cfg):
    route = respx.post("https://test.local/mcp").mock(
        return_value=Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{}"}]}},
        )
    )
    client = McpHttpClient(cfg)
    client.call_tool("kb_search", {"query": "x"})
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer hc_test"
    assert sent.headers["user-agent"] == f"harbor-clerk-cli/{__version__}"


@respx.mock
def test_connection_error_raises_mcp_client_error(cfg):
    respx.post("https://test.local/mcp").mock(side_effect=ConnectionError("boom"))
    client = McpHttpClient(cfg)
    with pytest.raises(McpClientError) as exc:
        client.call_tool("kb_search", {"query": "x"})
    assert exc.value.kind == "connection"


@respx.mock
def test_403_cli_disabled_raises_typed_error(cfg):
    respx.post("https://test.local/mcp").mock(
        return_value=Response(
            403,
            json={"error": "cli_access_disabled", "hint": "Enable in System Settings → Integrations"},
        )
    )
    client = McpHttpClient(cfg)
    with pytest.raises(McpClientError) as exc:
        client.call_tool("kb_search", {"query": "x"})
    assert exc.value.kind == "cli_disabled"


@respx.mock
def test_401_raises_auth_error(cfg):
    respx.post("https://test.local/mcp").mock(
        return_value=Response(401, json={"error": "Unauthorized"}),
    )
    client = McpHttpClient(cfg)
    with pytest.raises(McpClientError) as exc:
        client.call_tool("kb_search", {"query": "x"})
    assert exc.value.kind == "auth"
```

If `respx` is not yet a dev dep, add it: edit `pyproject.toml` (under the test/dev extras) and add `respx>=0.21`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_client.py -v`
Expected: All 5 fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the client**

Create `src/harbor_clerk/cli/client.py`:

```python
"""MCP-over-HTTP JSON-RPC client for the harbor-clerk CLI."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from harbor_clerk.cli import __version__
from harbor_clerk.cli.config import CliConfig


ErrorKind = Literal["connection", "auth", "cli_disabled", "http", "protocol"]


@dataclass
class McpClientError(Exception):
    kind: ErrorKind
    message: str
    status_code: int | None = None
    body: Any = None

    def __str__(self) -> str:
        return self.message


class McpHttpClient:
    """Synchronous JSON-RPC over HTTP client for POST /mcp."""

    def __init__(self, config: CliConfig) -> None:
        self._config = config
        verify = not config.insecure
        self._http = httpx.Client(
            base_url=config.url,
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=verify,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "User-Agent": f"harbor-clerk-cli/{__version__}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        request_id = str(uuid.uuid4())
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            resp = self._http.post("/mcp", json=body)
        except (httpx.ConnectError, httpx.TimeoutException, ConnectionError) as e:
            raise McpClientError(kind="connection", message=str(e)) from e

        if resp.status_code == 401:
            raise McpClientError(
                kind="auth",
                message="Authentication failed (HTTP 401). Check HARBOR_CLERK_API_KEY.",
                status_code=401,
                body=_safe_json(resp),
            )
        if resp.status_code == 403:
            payload = _safe_json(resp) or {}
            if isinstance(payload, dict) and payload.get("error") == "cli_access_disabled":
                raise McpClientError(
                    kind="cli_disabled",
                    message=payload.get(
                        "hint",
                        "CLI access disabled. Enable in System Settings → Integrations.",
                    ),
                    status_code=403,
                    body=payload,
                )
            raise McpClientError(
                kind="http",
                message=f"HTTP 403: {payload}",
                status_code=403,
                body=payload,
            )
        if resp.status_code >= 400:
            raise McpClientError(
                kind="http",
                message=f"HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )

        envelope = _safe_json(resp)
        if not isinstance(envelope, dict):
            raise McpClientError(kind="protocol", message="Non-JSON response body.")
        if "error" in envelope:
            raise McpClientError(
                kind="protocol",
                message=f"JSON-RPC error: {envelope['error']}",
                body=envelope,
            )

        result = envelope.get("result", {})
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text  # tool returned plain text
        return result

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "McpHttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _safe_json(resp) -> Any:
    try:
        return resp.json()
    except Exception:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_client.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/cli/client.py tests/cli/test_client.py pyproject.toml
git commit -m "feat(cli): MCP-over-HTTP JSON-RPC client with typed errors"
```

---

### Task 7: Output rendering (JSON / text / TTY detection)

**Files:**
- Create: `src/harbor_clerk/cli/output.py`
- Create: `tests/cli/test_output.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_output.py`:

```python
import io
import json
import pytest

from harbor_clerk.cli.output import (
    OutputMode,
    resolve_mode,
    render,
)


def test_resolve_mode_tty_defaults_to_text():
    assert resolve_mode(force_json=False, fmt=None, isatty=True) == OutputMode.TEXT


def test_resolve_mode_non_tty_defaults_to_json():
    assert resolve_mode(force_json=False, fmt=None, isatty=False) == OutputMode.JSON


def test_resolve_mode_json_flag_wins_over_tty():
    assert resolve_mode(force_json=True, fmt=None, isatty=True) == OutputMode.JSON


def test_resolve_mode_format_text_wins_over_pipe():
    assert resolve_mode(force_json=False, fmt="text", isatty=False) == OutputMode.TEXT


def test_render_json_emits_indented_json():
    buf = io.StringIO()
    render({"a": 1}, mode=OutputMode.JSON, command="search", stream=buf)
    assert json.loads(buf.getvalue()) == {"a": 1}


def test_render_text_search_results_uses_pretty_printer():
    buf = io.StringIO()
    payload = {
        "results": [
            {"chunk_id": "c1", "doc_id": "d1", "title": "Doc A", "page": 1,
             "snippet": "hello world", "score": 0.9, "language": "en", "citation": "Doc A, p.1"},
        ],
        "possible_conflict": False,
    }
    render(payload, mode=OutputMode.TEXT, command="search", stream=buf)
    out = buf.getvalue()
    assert "Doc A" in out
    assert "hello world" in out
    assert "0.9" in out or "0.90" in out


def test_render_text_falls_back_to_json_for_unknown_command():
    buf = io.StringIO()
    render({"foo": "bar"}, mode=OutputMode.TEXT, command="unknown-command", stream=buf)
    # No pretty-printer for unknown command → indented JSON to keep output usable.
    assert "foo" in buf.getvalue()
    assert "bar" in buf.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_output.py -v`
Expected: All 7 fail with `ModuleNotFoundError`.

- [ ] **Step 3: Implement output rendering**

Create `src/harbor_clerk/cli/output.py`:

```python
"""Output rendering for the harbor-clerk CLI."""
from __future__ import annotations

import enum
import json
import sys
from typing import Any, Callable, TextIO


class OutputMode(enum.Enum):
    JSON = "json"
    TEXT = "text"


def resolve_mode(*, force_json: bool, fmt: str | None, isatty: bool) -> OutputMode:
    if fmt == "json" or force_json:
        return OutputMode.JSON
    if fmt == "text":
        return OutputMode.TEXT
    return OutputMode.TEXT if isatty else OutputMode.JSON


# Per-command text pretty-printers. Unknown commands fall back to JSON.
_TEXT_RENDERERS: dict[str, Callable[[Any, TextIO], None]] = {}


def register_text_renderer(command: str):
    def deco(fn: Callable[[Any, TextIO], None]):
        _TEXT_RENDERERS[command] = fn
        return fn
    return deco


def render(payload: Any, *, mode: OutputMode, command: str, stream: TextIO | None = None) -> None:
    stream = stream or sys.stdout
    if mode == OutputMode.JSON:
        json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
        return

    renderer = _TEXT_RENDERERS.get(command)
    if renderer is None:
        json.dump(payload, stream, indent=2, ensure_ascii=False, default=str)
        stream.write("\n")
        return
    renderer(payload, stream)


# --- Search results pretty-printer ---


@register_text_renderer("search")
def _render_search(payload: Any, stream: TextIO) -> None:
    if not isinstance(payload, dict):
        stream.write(repr(payload) + "\n")
        return
    results = payload.get("results", [])
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        citation = r.get("citation", "")
        score = r.get("score")
        snippet = (r.get("snippet") or "").strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:200] + "…"
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "?"
        stream.write(f"{i}. {title}  [{score_str}]  {citation}\n")
        stream.write(f"   {snippet}\n\n")
    if payload.get("possible_conflict"):
        stream.write("⚠  possible_conflict=true — top hits disagree across documents\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_output.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/cli/output.py tests/cli/test_output.py
git commit -m "feat(cli): output mode resolution + text renderer registry"
```

---

### Task 8: Error → exit-code mapping

**Files:**
- Create: `src/harbor_clerk/cli/errors.py`
- Create: `tests/cli/test_errors.py`
- Modify: `src/harbor_clerk/cli/main.py` — wire the error handler

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_errors.py`:

```python
import io
import json
import pytest

from harbor_clerk.cli.client import McpClientError
from harbor_clerk.cli.errors import EXIT_CONNECTION, EXIT_CLI_DISABLED, EXIT_AUTH, EXIT_HTTP, EXIT_PROTOCOL
from harbor_clerk.cli.errors import map_client_error_to_exit, write_error


def test_connection_error_maps_to_2():
    err = McpClientError(kind="connection", message="ECONNREFUSED")
    assert map_client_error_to_exit(err) == EXIT_CONNECTION == 2


def test_cli_disabled_maps_to_3():
    err = McpClientError(kind="cli_disabled", message="...")
    assert map_client_error_to_exit(err) == EXIT_CLI_DISABLED == 3


def test_auth_maps_to_4():
    err = McpClientError(kind="auth", message="bad key")
    assert map_client_error_to_exit(err) == EXIT_AUTH == 4


def test_http_maps_to_5():
    err = McpClientError(kind="http", message="500")
    assert map_client_error_to_exit(err) == EXIT_HTTP == 5


def test_protocol_maps_to_5():
    err = McpClientError(kind="protocol", message="bad json-rpc")
    assert map_client_error_to_exit(err) == EXIT_PROTOCOL == 5


def test_write_error_text_mode_goes_to_stderr():
    buf = io.StringIO()
    err = McpClientError(kind="connection", message="could not connect to https://localhost")
    write_error(err, json_mode=False, stream=buf)
    out = buf.getvalue()
    assert "could not connect" in out


def test_write_error_json_mode_is_structured():
    buf = io.StringIO()
    err = McpClientError(kind="auth", message="bad key", status_code=401, body={"error": "Unauthorized"})
    write_error(err, json_mode=True, stream=buf)
    parsed = json.loads(buf.getvalue())
    assert parsed["error_kind"] == "auth"
    assert parsed["message"] == "bad key"
    assert parsed["status_code"] == 401
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_errors.py -v`
Expected: All fail (ModuleNotFoundError).

- [ ] **Step 3: Implement the mapping**

Create `src/harbor_clerk/cli/errors.py`:

```python
"""Exit-code mapping + structured error output for the harbor-clerk CLI."""
from __future__ import annotations

import json
import sys
from typing import TextIO

from harbor_clerk.cli.client import McpClientError


EXIT_OK = 0
EXIT_USAGE = 1  # argparse default for bad usage
EXIT_CONNECTION = 2
EXIT_CLI_DISABLED = 3
EXIT_AUTH = 4
EXIT_HTTP = 5
EXIT_PROTOCOL = 5  # protocol failures collapsed into the HTTP bucket


_EXIT_FOR_KIND = {
    "connection": EXIT_CONNECTION,
    "cli_disabled": EXIT_CLI_DISABLED,
    "auth": EXIT_AUTH,
    "http": EXIT_HTTP,
    "protocol": EXIT_PROTOCOL,
}


def map_client_error_to_exit(err: McpClientError) -> int:
    return _EXIT_FOR_KIND.get(err.kind, EXIT_HTTP)


def write_error(err: McpClientError, *, json_mode: bool, stream: TextIO | None = None) -> None:
    stream = stream or sys.stderr
    if json_mode:
        json.dump(
            {
                "error_kind": err.kind,
                "message": err.message,
                "status_code": err.status_code,
                "body": err.body,
            },
            stream,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        stream.write("\n")
    else:
        stream.write(f"harbor-clerk: {err.message}\n")
```

Modify `src/harbor_clerk/cli/main.py` to add a top-level handler that catches `McpClientError`. Replace the `main` function body with:

```python
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return handler(args)
    except McpClientError as err:
        json_mode = bool(args.json) or args.format == "json"
        write_error(err, json_mode=json_mode)
        return map_client_error_to_exit(err)
    except ValueError as err:
        # e.g. resolve_config — missing API key
        sys.stderr.write(f"harbor-clerk: {err}\n")
        return EXIT_USAGE
```

Add the imports near the top:

```python
from harbor_clerk.cli.client import McpClientError
from harbor_clerk.cli.errors import EXIT_USAGE, map_client_error_to_exit, write_error
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_errors.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/cli/errors.py tests/cli/test_errors.py src/harbor_clerk/cli/main.py
git commit -m "feat(cli): error → exit-code mapping (2 connection, 3 cli-disabled, 4 auth, 5 http)"
```

---

## Phase 3: Subcommands (4 tasks)

### Task 9: First subcommand — `search` (reference TDD pattern)

**Files:**
- Create: `src/harbor_clerk/cli/commands/search.py`
- Create: `tests/cli/test_commands_search.py`

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_commands_search.py`:

```python
import json
from unittest.mock import patch, MagicMock

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with patch("harbor_clerk.cli.commands.search.McpHttpClient") as MockClient, \
         patch("harbor_clerk.cli.commands.search.resolve_config") as MockResolve:
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_search_calls_kb_search_with_query_and_defaults(capsys):
    rc, client = _run(["search", "termination", "--json"], {"results": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_search",
        {"query": "termination", "k": 10, "offset": 0, "detail": "full"},
    )
    out = capsys.readouterr().out
    assert json.loads(out) == {"results": []}


def test_search_forwards_optional_filters(capsys):
    rc, client = _run(
        ["search", "force majeure", "--k", "5", "--detail", "brief",
         "--language", "en", "--after", "2026-01-01", "--json"],
        {"results": []},
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["k"] == 5
    assert args["detail"] == "brief"
    assert args["language"] == "en"
    assert args["after"] == "2026-01-01"


def test_search_text_mode_prints_human_readable(capsys):
    payload = {
        "results": [{
            "chunk_id": "c1", "doc_id": "d1", "title": "Contract A",
            "page": 4, "snippet": "shall terminate", "score": 0.87,
            "language": "en", "citation": "Contract A, p.4",
        }],
        "possible_conflict": False,
    }
    rc, _client = _run(["search", "terminate", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Contract A" in out
    assert "shall terminate" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/cli/test_commands_search.py -v`
Expected: All 3 fail with `ModuleNotFoundError` or `AttributeError` on the search module.

- [ ] **Step 3: Implement the subcommand**

Create `src/harbor_clerk/cli/commands/search.py`:

```python
"""harbor-clerk search — hybrid FTS + vector search."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harbor_clerk.cli.client import McpHttpClient
from harbor_clerk.cli.config import resolve_config
from harbor_clerk.cli.output import OutputMode, render, resolve_mode


_HELP_PATH = Path(__file__).parent.parent / "help" / "search.txt"


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    description = _HELP_PATH.read_text() if _HELP_PATH.exists() else "Hybrid FTS + vector search."
    p = subparsers.add_parser(
        "search",
        help="Hybrid FTS + vector search across the corpus",
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("query", help="The search query")
    p.add_argument("-k", "--k", type=int, default=10, help="Number of results (default: 10, max: 50)")
    p.add_argument("-o", "--offset", type=int, default=0, help="Pagination offset (default: 0)")
    p.add_argument("-d", "--detail", choices=["full", "brief"], default="full")
    p.add_argument("--brief-chars", type=int, default=None, help="When --detail=brief, chars per snippet")
    p.add_argument("--doc-id", help="Restrict search to one document UUID")
    p.add_argument("--doc-ids", help="Comma-separated doc UUIDs")
    p.add_argument("--after", help="YYYY-MM-DD; only docs ingested after this date")
    p.add_argument("--before", help="YYYY-MM-DD; only docs ingested before this date")
    p.add_argument("--language", choices=["en", "fr"])
    p.add_argument("--mime-type", help="Restrict by MIME type (e.g. application/pdf)")
    p.add_argument("--faceted", action="store_true", help="Include facet counts in response")
    p.set_defaults(_handler=run)


def run(args) -> int:
    arguments: dict = {
        "query": args.query,
        "k": args.k,
        "offset": args.offset,
        "detail": args.detail,
    }
    if args.brief_chars is not None:
        arguments["brief_chars"] = args.brief_chars
    if args.doc_id:
        arguments["doc_id"] = args.doc_id
    if args.doc_ids:
        arguments["doc_ids"] = [d.strip() for d in args.doc_ids.split(",") if d.strip()]
    if args.after:
        arguments["after"] = args.after
    if args.before:
        arguments["before"] = args.before
    if args.language:
        arguments["language"] = args.language
    if args.mime_type:
        arguments["mime_type"] = args.mime_type
    if args.faceted:
        arguments["faceted"] = True

    cfg = resolve_config(url=args.url, api_key=args.api_key, insecure=args.insecure)
    mode = resolve_mode(force_json=bool(args.json), fmt=args.format, isatty=sys.stdout.isatty())
    with McpHttpClient(cfg) as client:
        payload = client.call_tool("kb_search", arguments)
    render(payload, mode=mode, command="search")
    return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/cli/test_commands_search.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/cli/commands/search.py tests/cli/test_commands_search.py
git commit -m "feat(cli): search subcommand (reference implementation)"
```

---

### Task 10: Subcommands `read-passages`, `expand-context`, `read-document`, `get-document`, `ingest-status`

For each subcommand below, create the file at `src/harbor_clerk/cli/commands/<name_underscored>.py` and the corresponding test at `tests/cli/test_commands_<name_underscored>.py`, following exactly the pattern from Task 9 (`search.py` + `test_commands_search.py`). The structure of each file is identical to Task 9; only the parser arguments, the tool name passed to `call_tool`, and the test assertions differ.

#### 10a. `read-passages`

**Files:**
- Create: `src/harbor_clerk/cli/commands/read_passages.py`
- Create: `tests/cli/test_commands_read_passages.py`

**MCP tool:** `kb_read_passages`

**Parser args:**
- `chunk_ids` — positional, `nargs="+"`, list of chunk UUIDs to read
- `--include-meta` — `action="store_true"` — include passage metadata in response

**Argument mapping (in `run`):**
```python
arguments = {"chunk_ids": list(args.chunk_ids)}
if args.include_meta:
    arguments["include_meta"] = True
```

**Tests (3):**
1. `test_read_passages_calls_kb_read_passages_with_chunk_ids` — pass 3 chunk IDs, assert tool name + args
2. `test_read_passages_include_meta_forwarded` — `--include-meta` → `include_meta=True` in args
3. `test_read_passages_no_chunk_ids_exits_1` — invoke with no positional args, assert rc==1 and stderr contains "required"

- [ ] **Steps 1–5** (test fails → implement → tests pass → commit) per the Task 9 pattern.

```bash
git commit -m "feat(cli): read-passages subcommand"
```

#### 10b. `expand-context`

**MCP tool:** `kb_expand_context`

**Parser args:**
- `chunk_id` — positional, single UUID
- `-n`, `--n` — int, default 2 — chunks before/after

**Argument mapping:**
```python
arguments = {"chunk_id": args.chunk_id, "n": args.n}
```

**Tests (2):**
1. `test_expand_context_defaults_n_to_2`
2. `test_expand_context_n_flag_forwarded`

Commit: `feat(cli): expand-context subcommand`

#### 10c. `read-document`

**MCP tool:** `kb_read_document`

**Parser args:**
- `doc_id` — positional
- `--page` — int, default 1, "1-indexed page within paginated read"
- `--page-size` — int, default 5, "chunks per page"

**Argument mapping:**
```python
arguments = {"doc_id": args.doc_id, "page": args.page, "page_size": args.page_size}
```

**Tests (2):**
1. `test_read_document_defaults`
2. `test_read_document_page_and_size_forwarded`

Commit: `feat(cli): read-document subcommand`

#### 10d. `get-document`

**MCP tool:** `kb_get_document`

**Parser args:**
- `doc_id` — positional

**Argument mapping:**
```python
arguments = {"doc_id": args.doc_id}
```

**Tests (1):**
1. `test_get_document_calls_kb_get_document_with_doc_id`

Commit: `feat(cli): get-document subcommand`

#### 10e. `ingest-status`

**MCP tool:** `kb_ingest_status`

**Parser args:**
- `doc_id` — positional

**Argument mapping:**
```python
arguments = {"doc_id": args.doc_id}
```

**Tests (1):**
1. `test_ingest_status_calls_kb_ingest_status_with_doc_id`

Commit: `feat(cli): ingest-status subcommand`

---

### Task 11: Subcommands `batch-search`, `find-related`, `list-recent`, `corpus-overview`, `document-outline`

Same pattern as Task 10. Create one impl file + one test file per command, mirroring Task 9.

#### 11a. `batch-search`

**MCP tool:** `kb_batch_search`

**Parser args:**
- `queries` — positional, `nargs="+"` — list of query strings
- `-k`, `--k` — int, default 5
- `-d`, `--detail` — choices `["full", "brief"]`, default `"brief"`

**Argument mapping:**
```python
arguments = {"queries": list(args.queries), "k": args.k, "detail": args.detail}
```

**Tests (2):**
1. `test_batch_search_multiple_queries_forwarded`
2. `test_batch_search_defaults`

Commit: `feat(cli): batch-search subcommand`

#### 11b. `find-related`

**MCP tool:** `kb_find_related`

**Parser args:**
- `doc_id` — positional
- `-k`, `--k` — int, default 5

Commit: `feat(cli): find-related subcommand`

#### 11c. `list-recent`

**MCP tool:** `kb_list_recent`

**Parser args:**
- `--limit` — int, default 20

Commit: `feat(cli): list-recent subcommand`

#### 11d. `corpus-overview`

**MCP tool:** `kb_corpus_overview`

**Parser args:**
- `--limit` — int, default 50

Commit: `feat(cli): corpus-overview subcommand`

#### 11e. `document-outline`

**MCP tool:** `kb_document_outline`

**Parser args:**
- `doc_id` — positional

Commit: `feat(cli): document-outline subcommand`

---

### Task 12: Subcommands `entity-search`, `entity-overview`, `entity-cooccurrence`, `reprocess`, `system-health`

Same pattern as Task 10.

#### 12a. `entity-search`

**MCP tool:** `kb_entity_search`

**Parser args:**
- `query` — positional, the entity name or substring
- `--type` — optional, entity type filter (e.g. PERSON, ORG)
- `-k`, `--k` — int, default 20

**Argument mapping:**
```python
arguments = {"query": args.query, "k": args.k}
if args.type:
    arguments["type"] = args.type
```

Commit: `feat(cli): entity-search subcommand`

#### 12b. `entity-overview`

**MCP tool:** `kb_entity_overview`

**Parser args:**
- `--doc-id` — optional; if set, scope to one document

**Argument mapping:**
```python
arguments = {}
if args.doc_id:
    arguments["doc_id"] = args.doc_id
```

Commit: `feat(cli): entity-overview subcommand`

#### 12c. `entity-cooccurrence`

**MCP tool:** `kb_entity_cooccurrence`

**Parser args:**
- `entity` — positional, the entity name
- `-k`, `--k` — int, default 20

Commit: `feat(cli): entity-cooccurrence subcommand`

#### 12d. `reprocess`

**MCP tool:** `kb_reprocess`

**Parser args:**
- `doc_id` — positional

Commit: `feat(cli): reprocess subcommand`

#### 12e. `system-health`

**MCP tool:** `kb_system_health`

**Parser args:** none.

**Argument mapping:**
```python
arguments = {}
```

Commit: `feat(cli): system-health subcommand`

---

## Phase 4: Help text (2 tasks)

### Task 13: Comprehensive help for `search` (reference template) + help loader

**Files:**
- Create: `src/harbor_clerk/cli/help/__init__.py`
- Create: `src/harbor_clerk/cli/help/search.txt`

- [ ] **Step 1: Create the help loader module**

Create `src/harbor_clerk/cli/help/__init__.py`:

```python
"""Loader for per-subcommand long-help text files."""
from __future__ import annotations

from pathlib import Path


def load(command: str) -> str:
    """Load the help text for a command, falling back to an empty string."""
    path = Path(__file__).parent / f"{command}.txt"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Write the reference help file**

Create `src/harbor_clerk/cli/help/search.txt`:

```
harbor-clerk search — hybrid FTS + vector search

DESCRIPTION
  Runs a hybrid retrieval combining PostgreSQL full-text search (bilingual,
  English + French) and pgvector cosine similarity over the corpus. Scores
  are normalized and merged per-chunk with a small OCR-confidence boost.
  Results include citations (doc_id, page, chunk_id) usable with
  `read-passages` and `expand-context`.

  If top hits have similar scores across multiple documents, the response
  includes `possible_conflict: true` and a `conflict_sources` array — quote
  both sides to the user.

USAGE
  harbor-clerk search <query> [options]

OPTIONS
  -k, --k INT               Number of results (default: 10, max: 50)
  -o, --offset INT          Pagination offset (default: 0)
  -d, --detail full|brief   Result detail level (default: full)
      --brief-chars INT     When --detail=brief, chars per snippet (default: 200)
      --doc-id UUID         Restrict search to one document
      --doc-ids UUID,UUID   Restrict search to multiple documents
      --after YYYY-MM-DD    Only docs ingested after this date
      --before YYYY-MM-DD   Only docs ingested before this date
      --language en|fr      Restrict to one language
      --mime-type MIME      Restrict by MIME type (e.g. application/pdf)
      --faceted             Include facet counts in response

RETURNS (JSON)
  {
    "results": [
      {
        "chunk_id":   "uuid",        // pass to read-passages or expand-context
        "doc_id":     "uuid",        // document identifier
        "title":      "string",      // document title
        "page":       42,            // 1-indexed page (or null for non-paginated)
        "snippet":    "string",      // matched text with context
        "score":      0.87,          // hybrid score, higher = better
        "language":   "en" | "fr",
        "citation":   "Title, p.42"  // human-readable citation
      }
    ],
    "possible_conflict": false,
    "conflict_sources": []           // present only when possible_conflict=true
  }

EXAMPLES
  # Basic search
  harbor-clerk search "termination clause"

  # Restrict to PDFs ingested in the last month, JSON to jq
  harbor-clerk search "force majeure" \
    --mime-type application/pdf \
    --after 2026-04-23 | jq '.results[] | {title, page, snippet}'

  # Two-stage: search then expand the best hit
  CHUNK=$(harbor-clerk search "indemnification" -k 1 | jq -r '.results[0].chunk_id')
  harbor-clerk expand-context "$CHUNK" -n 3

  # Faceted overview before drilling in
  harbor-clerk search "audit" --faceted | jq '.facets'

COMMON MISTAKES
  - Passing a chunk_id as --doc-id (chunk_id and doc_id are distinct UUIDs).
  - Forgetting --detail=brief on large k — default `full` returns the entire
    matching chunk text, which can be 1–2KB per result.
  - Using --after/--before with timestamps; only dates (YYYY-MM-DD) are accepted.

SEE ALSO
  read-passages, expand-context, batch-search, find-related
```

- [ ] **Step 3: Verify `--help` for search includes the long text**

Run: `uv run python -m harbor_clerk.cli.main search --help`
Expected: the long help text above is printed.

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/cli/help/__init__.py src/harbor_clerk/cli/help/search.txt
git commit -m "feat(cli): comprehensive help text for search (reference template)"
```

---

### Task 14: Help text for the remaining 15 subcommands

For each of the 15 remaining subcommands, create `src/harbor_clerk/cli/help/<name>.txt` following the **exact 7-section structure** of `search.txt` from Task 13:

1. **One-line title**
2. **DESCRIPTION** — paragraph(s) describing what the tool does and what guarantees it provides
3. **USAGE** — one-line signature
4. **OPTIONS** — every flag with type, default, constraints
5. **RETURNS (JSON)** — full JSON schema with field-by-field annotations
6. **EXAMPLES** — 3–5: golden path, edge case, shell composition example, any two-stage idioms
7. **COMMON MISTAKES** — known footguns
8. **SEE ALSO** — related subcommands

Source of truth for what each tool does, its arguments, and its return shape: `src/harbor_clerk/mcp_server.py`. Read the docstring + Pydantic field types + the actual return-payload construction code to draft each help file accurately.

Split into four commits for review-ability:

- [ ] **14a — Search-family (4 files):** `batch-search.txt`, `read-passages.txt`, `expand-context.txt`, `find-related.txt`. Commit: `docs(cli): help text — search-family subcommands`
- [ ] **14b — Document-family (5 files):** `read-document.txt`, `get-document.txt`, `document-outline.txt`, `list-recent.txt`, `corpus-overview.txt`. Commit: `docs(cli): help text — document-family subcommands`
- [ ] **14c — Entity-family (3 files):** `entity-search.txt`, `entity-overview.txt`, `entity-cooccurrence.txt`. Commit: `docs(cli): help text — entity-family subcommands`
- [ ] **14d — System (3 files):** `ingest-status.txt`, `reprocess.txt`, `system-health.txt`. Commit: `docs(cli): help text — system subcommands`

After each batch, spot-check with `uv run python -m harbor_clerk.cli.main <cmd> --help` to confirm rendering.

---

## Phase 5: Integrations UI (2 tasks)

### Task 15: Frontend `useSystemConfig` exposes `enableCliAccess`

**Files:**
- Modify: `frontend/src/hooks/useSystemConfig.ts`

- [ ] **Step 1: Read the existing hook**

Open `frontend/src/hooks/useSystemConfig.ts` and locate the `allowSourceDownload` field handling.

- [ ] **Step 2: Add a parallel `enableCliAccess` field**

In the same hook, parallel to the `allowSourceDownload` field:

```typescript
// In the SystemConfig type
enableCliAccess: boolean

// In the fetch handler, alongside allowSourceDownload
enableCliAccess: Boolean(data.enable_cli_access),
```

- [ ] **Step 3: Add a test**

If `useSystemConfig.test.ts` exists, extend it. Otherwise add a minimal type-check that the new field appears in the default state.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/hooks/useSystemConfig.ts
git commit -m "feat(frontend): expose enableCliAccess in useSystemConfig"
```

---

### Task 16: Integrations page CLI access card

**Files:**
- Create: `frontend/src/components/CliAccessCard.tsx`
- Modify: `frontend/src/pages/IntegrationsPage.tsx`

- [ ] **Step 1: Create the component**

Create `frontend/src/components/CliAccessCard.tsx`:

```tsx
import { Card } from './Card'

const SKILL_MARKDOWN = `---
name: harbor-clerk
description: Search and read documents from a local Harbor Clerk knowledge base. Use when the user references their personal documents, contracts, notes, emails, or asks "what did I store about X?"
---

# Harbor Clerk — extended memory for agents

Harbor Clerk is a local document corpus with hybrid FTS+vector search and citation-preserving reads. This skill exposes it via the \`harbor-clerk\` CLI.

## First step: discover the surface
Run \`harbor-clerk --help\` for the full command list, then \`harbor-clerk <cmd> --help\` for any specific command. The help is comprehensive — JSON return shapes, examples, common mistakes are all there.

## Three patterns you'll use most

1. **Search → expand**: \`harbor-clerk search "..."\` then \`harbor-clerk expand-context <chunk_id> -n 3\` on the best hit.
2. **Read a known document**: \`harbor-clerk read-document <doc_id>\` for full text with pagination.
3. **Check ingest status before searching for new content**: \`harbor-clerk ingest-status <doc_id>\` returns per-stage state.

## What you can trust
- Every search result includes a \`citation\` field — quote it back to the user.
- \`possible_conflict: true\` means top hits disagree across documents; surface both sources.
- The CLI exits non-zero on failure. Exit code 3 specifically means an admin has disabled CLI access — tell the user.
`

function CopyButton({ text }: { text: string }) {
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // best-effort
    }
  }
  return (
    <button
      onClick={onClick}
      className="text-xs px-2 py-1 rounded border hover:bg-gray-100 dark:hover:bg-gray-800"
    >
      Copy
    </button>
  )
}

export function CliAccessCard({
  enabled,
  envVarHint,
}: {
  enabled: boolean
  envVarHint: string
}) {
  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold mb-2">Agentic CLI</h3>
      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Expose Harbor Clerk to OpenClaw, Claude Code, Codex, and other CLI-orchestrating
        agent harnesses via a <code className="text-xs">harbor-clerk</code> command.
      </p>

      <div className="flex items-center gap-2 mb-4">
        <span
          className={`inline-block w-2 h-2 rounded-full ${
            enabled ? 'bg-green-500' : 'bg-gray-400'
          }`}
        />
        <span className="text-sm">
          CLI access is {enabled ? <strong>enabled</strong> : <strong>disabled</strong>}
        </span>
      </div>

      {!enabled && (
        <div className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          To enable, set <code className="text-xs">{envVarHint}</code> and restart the API service.
        </div>
      )}

      <h4 className="text-sm font-semibold mt-4 mb-2">Skill markdown (copy into your harness)</h4>
      <div className="relative">
        <pre className="text-xs bg-gray-100 dark:bg-gray-900 rounded p-3 overflow-x-auto">
          <code>{SKILL_MARKDOWN}</code>
        </pre>
        <div className="absolute top-2 right-2">
          <CopyButton text={SKILL_MARKDOWN} />
        </div>
      </div>
    </Card>
  )
}
```

- [ ] **Step 2: Embed it in the Integrations page**

In `frontend/src/pages/IntegrationsPage.tsx`, find an appropriate insertion point (next to other connector cards) and add:

```tsx
import { CliAccessCard } from '../components/CliAccessCard'

// inside the component, alongside useSystemConfig usage:
const { enableCliAccess } = useSystemConfig()

// inside the JSX, in the appropriate section:
<CliAccessCard
  enabled={enableCliAccess}
  envVarHint="ENABLE_CLI_ACCESS=true"
/>
```

- [ ] **Step 3: Manually verify in dev**

Run the dev server (project-specific command — check `frontend/package.json` `scripts.dev`) and confirm the card renders, the toggle label is correct, and the Copy button works.

- [ ] **Step 4: Run frontend type-check + lint**

Run: `cd frontend && npm run type-check && npm run lint`
Expected: 0 errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/CliAccessCard.tsx frontend/src/pages/IntegrationsPage.tsx
git commit -m "feat(frontend): Integrations page CLI access card with skill copy block"
```

---

## Phase 6: macOS shim installer (1 task — Swift)

### Task 17: macOS `/usr/local/bin/harbor-clerk` shim installer

**Files:**
- Create: `macos/HarborClerkServer/Sources/HarborClerkServer/CliShimInstaller.swift`
- Modify: the menubar preferences pane to add an "Install CLI shim" button + status
- Modify: the relevant Swift module to invoke the installer on first launch when `enableCliAccess` is true

**Context:** The shim is a tiny shell script at `/usr/local/bin/harbor-clerk` that invokes the bundled Python with the CLI entry point. The installer prompts the user via `AuthorizationServices` because writes to `/usr/local/bin` need admin on macOS.

- [ ] **Step 1: Define the shim contents**

The shim is a static script:

```sh
#!/bin/sh
# harbor-clerk shim — installed by Harbor Clerk Server
HC_PYTHON="/Applications/Harbor Clerk Server.app/Contents/Resources/python/bin/python3"
HC_VENV_SITE="/Applications/Harbor Clerk Server.app/Contents/Resources/venv/lib/python3.12/site-packages"
PYTHONPATH="$HC_VENV_SITE:$PYTHONPATH" exec "$HC_PYTHON" -m harbor_clerk.cli.main "$@"
```

Verify the actual paths against the existing app bundle layout — look at how `harbor-clerk-api` and `harbor-clerk-worker` are invoked from Swift today (`grep -rn "harbor-clerk-api" macos/`) and mirror that.

- [ ] **Step 2: Create the Swift installer**

Create `macos/HarborClerkServer/Sources/HarborClerkServer/CliShimInstaller.swift`. Use `AuthorizationServices` (via `AuthorizationExecuteWithPrivileges` — deprecated but still works on current macOS, OR a more modern Helper-tool pattern if the project already uses one). Inspect the existing macOS codebase for the established pattern; do not invent a new one.

Public surface:

```swift
enum CliShimInstallStatus {
    case installed
    case notInstalled
    case mismatchedVersion(installedVersion: String, bundledVersion: String)
    case error(String)
}

final class CliShimInstaller {
    static func currentStatus() -> CliShimInstallStatus
    static func install() async -> CliShimInstallStatus  // prompts for admin
    static func uninstall() async -> CliShimInstallStatus
}
```

- [ ] **Step 3: Hook into the preferences pane**

In the preferences/settings window, add a "CLI Shim" row:
- Status badge (installed / not installed / mismatch)
- Install / Reinstall / Uninstall button depending on state

- [ ] **Step 4: Expose status through the API for the frontend**

In `src/harbor_clerk/api/routes/system.py`, add `cli_install_status` to the system-health response when running on macOS. The Swift side can write a small JSON file (e.g., `~/Library/Application Support/Harbor Clerk/cli-shim-status.json`) that the Python API reads, or expose it via the existing Swift→Python bridge if one is present.

- [ ] **Step 5: Build the macOS app and smoke-test**

Run: `make -C macos build` (or whatever the build target is — check `macos/Makefile`).
Expected: build succeeds; launching the app, opening preferences, and clicking "Install CLI shim" produces a working `/usr/local/bin/harbor-clerk` that responds to `--version`.

- [ ] **Step 6: Commit**

```bash
git add macos/HarborClerkServer/Sources/HarborClerkServer/CliShimInstaller.swift macos/...
git commit -m "feat(macos): CLI shim installer with AuthorizationServices prompt"
```

> **Note:** if Swift work is out of scope for whoever executes this plan, this task can land in a follow-up PR. The CLI is usable on macOS via `python -m harbor_clerk.cli.main ...` even without the shim. Document this in the PR description.

---

## Phase 7: Skill + docs (2 tasks)

### Task 18: Commit `skills/harbor-clerk/SKILL.md`

**Files:**
- Create: `skills/harbor-clerk/SKILL.md`

- [ ] **Step 1: Create the file**

Create `skills/harbor-clerk/SKILL.md` with the exact content embedded in `CliAccessCard.tsx`'s `SKILL_MARKDOWN` constant. Keep these two in sync — if the markdown changes here, update the frontend constant too (consider a future task to load the markdown from disk at build time to avoid drift).

```markdown
---
name: harbor-clerk
description: Search and read documents from a local Harbor Clerk knowledge base. Use when the user references their personal documents, contracts, notes, emails, or asks "what did I store about X?"
---

# Harbor Clerk — extended memory for agents

Harbor Clerk is a local document corpus with hybrid FTS+vector search and citation-preserving reads. This skill exposes it via the `harbor-clerk` CLI.

## First step: discover the surface
Run `harbor-clerk --help` for the full command list, then `harbor-clerk <cmd> --help` for any specific command. The help is comprehensive — JSON return shapes, examples, common mistakes are all there.

## Three patterns you'll use most

1. **Search → expand**: `harbor-clerk search "..."` then `harbor-clerk expand-context <chunk_id> -n 3` on the best hit.
2. **Read a known document**: `harbor-clerk read-document <doc_id>` for full text with pagination.
3. **Check ingest status before searching for new content**: `harbor-clerk ingest-status <doc_id>` returns per-stage state.

## What you can trust
- Every search result includes a `citation` field — quote it back to the user.
- `possible_conflict: true` means top hits disagree across documents; surface both sources.
- The CLI exits non-zero on failure. Exit code 3 specifically means an admin has disabled CLI access — tell the user.
```

- [ ] **Step 2: Commit**

```bash
git add skills/harbor-clerk/SKILL.md
git commit -m "docs(skills): in-repo harbor-clerk skill markdown (canonical source)"
```

---

### Task 19: README + CLAUDE.md updates

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add an "Agentic CLI" section to README**

Insert after the existing MCP section:

```markdown
## Agentic CLI

The `harbor-clerk` CLI exposes the same 16 retrieval tools as the MCP server, intended for CLI-orchestrating agent harnesses like OpenClaw, Claude Code, Codex, and Aider.

Setup:

```bash
export HARBOR_CLERK_API_KEY=hc_...   # mint in System Settings → API Keys
export ENABLE_CLI_ACCESS=true        # on the server side; restart required
harbor-clerk --help                  # full subcommand list
harbor-clerk search "..." --help     # man-page-class help per subcommand
```

CLI access is **off by default** and audit-logged as `request_type="cli_tool"`. See System Settings → Integrations for the copy-pasteable agent skill.
```

- [ ] **Step 2: Update CLAUDE.md Key API Surface**

In `CLAUDE.md`, in the "Key API Surface" section, after the MCP line, add:

```markdown
- CLI: `harbor-clerk <command>` — 16 subcommands mirroring MCP tools. Off by default (`ENABLE_CLI_ACCESS=true` to enable). Audit-logged as `request_type="cli_tool"`. Auth via `HARBOR_CLERK_API_KEY` env var.
```

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: README + CLAUDE.md note the new harbor-clerk CLI"
```

---

## Phase 8: End-to-end verification (1 task)

### Task 20: E2E smoke test + manual run

**Files:**
- Create: `tests/cli/test_e2e.py`

- [ ] **Step 1: Identify the existing E2E test pattern**

Run: `find tests -name "test_*.py" | xargs grep -l "create_app\|TestClient\|ASGITransport" | head -5`
Pick the pattern that runs the full app with a real DB (or the existing test-DB fixture). Mirror it.

- [ ] **Step 2: Write the E2E test**

Create `tests/cli/test_e2e.py` (adapt fixture names to the project's actual conventions):

```python
"""End-to-end smoke test: real CLI subprocess against real running app."""
import json
import subprocess
import sys
import pytest


pytestmark = pytest.mark.e2e


@pytest.fixture
async def live_server(seeded_app):
    """Spin up the FastAPI app on a real port. Adapt to project fixtures."""
    # Use the project's existing live-server fixture if one exists.
    # If not, this test can be skipped until a fixture is created.
    yield seeded_app  # placeholder — replace with the actual fixture


def _cli(args, env):
    return subprocess.run(
        [sys.executable, "-m", "harbor_clerk.cli.main", *args],
        capture_output=True, text=True, env=env,
    )


def test_e2e_cli_disabled_returns_exit_3(live_server, api_key, monkeypatch):
    env = {
        "HARBOR_CLERK_URL": live_server.url,
        "HARBOR_CLERK_API_KEY": api_key.plaintext,
        "HARBOR_CLERK_INSECURE_SKIP_VERIFY": "true",
        "ENABLE_CLI_ACCESS": "false",
        "PATH": "/usr/bin:/bin",
    }
    result = _cli(["search", "anything", "--json"], env=env)
    assert result.returncode == 3
    assert "cli_access_disabled" in result.stderr or "Integrations" in result.stderr


def test_e2e_cli_enabled_returns_results(live_server, api_key, monkeypatch):
    env = {
        "HARBOR_CLERK_URL": live_server.url,
        "HARBOR_CLERK_API_KEY": api_key.plaintext,
        "HARBOR_CLERK_INSECURE_SKIP_VERIFY": "true",
        "ENABLE_CLI_ACCESS": "true",
        "PATH": "/usr/bin:/bin",
    }
    result = _cli(["search", "the", "--json"], env=env)
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)
    assert "results" in body
```

If the project doesn't have a live-server fixture, mark this test `@pytest.mark.skip(reason="needs live-server fixture")` and create a follow-up issue rather than block this plan on it.

- [ ] **Step 3: Run the E2E test**

Run: `uv run pytest tests/cli/test_e2e.py -v -m e2e`
Expected: 2 passed (or both skipped with a clear reason).

- [ ] **Step 4: Manual end-to-end smoke**

In a real terminal (after a `docker compose up` or native-app start):

```bash
export HARBOR_CLERK_API_KEY=hc_<real-key>
export ENABLE_CLI_ACCESS=true
# restart the API service so the new env var is picked up
harbor-clerk --version           # → harbor-clerk-cli/0.1.0
harbor-clerk --help              # → full command list
harbor-clerk search --help       # → comprehensive help
harbor-clerk search "test"       # → text-mode results (TTY)
harbor-clerk search "test" --json | jq '.results | length'  # → JSON
harbor-clerk system-health       # → daemon status
```

Verify in System Settings → Audit Log that each call recorded a row with `request_type="cli_tool"`.

Toggle `ENABLE_CLI_ACCESS=false`, restart, run `harbor-clerk search "x"` → confirm exit code 3 and the user-friendly error.

- [ ] **Step 5: Commit (or note skipped test)**

```bash
git add tests/cli/test_e2e.py
git commit -m "test(cli): end-to-end smoke against live server"
```

---

## Self-Review (the plan author's checklist)

After writing this plan, the author ran the spec-coverage check:

| Spec section | Covered by |
|---|---|
| Goal — additive CLI alongside MCP | Whole plan |
| Server delta — `enable_cli_access` + UA gate + `request_type="cli_tool"` | Tasks 1, 2, 3 |
| Flat 16-subcommand surface | Tasks 9–12 |
| Config (env vars + flags) | Task 5 |
| Output (JSON / text / TTY) | Task 7 |
| Help (man-page-class per subcommand) | Tasks 13, 14 |
| Failure modes (exit codes 0–5) | Task 8 |
| Distribution — macOS shim | Task 17 |
| Distribution — Docker / pip | Task 4 (entry point); Task 19 (README docs) |
| Integrations page card + skill copy block | Tasks 15, 16 |
| In-repo skill at skills/harbor-clerk/SKILL.md | Task 18 |
| HC 1.0 publish to OpenClaw skill repo | **Not in this plan — captured in memory note `project_hc_1_0_skill_publish.md`** |

Type / signature consistency: `McpHttpClient`, `McpClientError`, `CliConfig`, `resolve_config`, `OutputMode`, `resolve_mode`, `render`, `register_text_renderer`, `add_parser`, `run`, `register_all`, exit-code constants — all named identically across every task they appear in.

Placeholder scan: no "TBD" or "implement appropriate X" present. Per-subcommand sections in Tasks 10–12 specify exact tool names, exact arg lists, and exact argument-mapping code — the engineer has enough to write the subcommand directly without filling in undefined details.
