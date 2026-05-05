# Email Ingestion — Stage 2: IMAP Sync Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Headless IMAP sync engine that connects to user-owned mail accounts, discovers labels, and continuously populates `watched_messages` rows for each watched label using IDLE + poll fallback. No Document creation yet (Stage 3); no UI (Stage 4). Operators interact via REST API.

**Architecture:** Async `aioimaplib` for the IMAP wire protocol (IDLE-capable). Per-label sync state machine running as supervised asyncio tasks under the existing `harbor-clerk-watcher` daemon, alongside the filesystem observer. Cursor state lives in `watched_labels.uidvalidity` + `last_uid_seen`. Lifecycle (un-labeled, server-deleted, account auth-error) handled via status transitions on the existing tables. New REST endpoints under `/api/mail/*` for account + label CRUD + test/rescan, all admin-gated.

**Tech Stack:** Python 3.12, `aioimaplib` (new dep), asyncio, FastAPI, SQLAlchemy 2.0 async, the existing `harbor-clerk-watcher` daemon entry point.

**Spec:** [`docs/superpowers/specs/2026-05-04-email-ingestion-design.md`](../specs/2026-05-04-email-ingestion-design.md)

**Builds on:** Stage 1 ([PR #281](https://github.com/r0shi/harborclerk/pull/281)) — `mail_accounts` / `watched_labels` / `watched_messages` schema, `MailAccount` / `WatchedLabel` / `WatchedMessage` models, `harbor_clerk.secrets` module (Cipher + Keychain).

**Implementation note:** This stage is headless — no UI, no Document creation. After Stage 2 ships, an admin can hand-craft a `mail_account` via `POST /api/mail/accounts`, watch a label via `POST /api/mail/labels`, and observe `watched_messages` rows accumulate as new mail arrives. The actual `.eml` parsing into Documents is Stage 3.

---

## File Structure

**New files:**
- `src/harbor_clerk/mail/__init__.py`
- `src/harbor_clerk/mail/exceptions.py` — typed errors (`AuthError`, `UidValidityChanged`, `IdleNotSupported`)
- `src/harbor_clerk/mail/imap_client.py` — thin async wrapper around `aioimaplib.IMAP4_SSL`
- `src/harbor_clerk/mail/labels.py` — `LIST` parsing → folder tree, system-folder detection
- `src/harbor_clerk/mail/cursor.py` — per-label cursor read/write helpers
- `src/harbor_clerk/mail/sync.py` — per-label state machine (initial → idle → reconnect → error)
- `src/harbor_clerk/mail/idle.py` — IDLE supervisor + 2-minute poll fallback
- `src/harbor_clerk/mail/lifecycle.py` — detect missing UIDs → mark `watched_messages.status='unlabeled'`
- `src/harbor_clerk/api/routes/mail.py` — REST endpoints: accounts CRUD + test, labels CRUD + rescan
- `src/harbor_clerk/api/schemas/mail.py` — Pydantic request/response schemas
- `src/harbor_clerk/watcher/mail_observer.py` — drives one sync task per active `watched_label`
- `tests/mail/__init__.py`
- `tests/mail/conftest.py` — fixtures: `MockIMAPClient`, fake account/label rows
- `tests/mail/test_imap_client.py`
- `tests/mail/test_labels.py`
- `tests/mail/test_cursor.py`
- `tests/mail/test_sync_initial.py`
- `tests/mail/test_sync_incremental.py`
- `tests/mail/test_sync_uidvalidity.py`
- `tests/mail/test_idle_polling.py`
- `tests/mail/test_lifecycle.py`
- `tests/mail/test_mail_observer.py`
- `tests/api/test_mail_routes.py`
- `tests/integration/test_mail_e2e_dovecot.py` — `@pytest.mark.integration` (skipped unless dovecot test fixture is available)

**Modified files:**
- `pyproject.toml` — add `aioimaplib>=1.1.0` to `dependencies`
- `src/harbor_clerk/api/app.py` — register the `mail` router
- `src/harbor_clerk/watcher/main.py` — launch `mail_observer` alongside filesystem `Observer`
- `src/harbor_clerk/watcher/notify.py` (or `db_listener.py`) — extend the per-channel `LISTEN` set with `mail_accounts_changed` / `watched_labels_changed`
- `pytest` configuration — add `integration` marker

---

## Task 1: aioimaplib dep + IMAPConnection skeleton

**Files:**
- Modify: `pyproject.toml`
- Create: `src/harbor_clerk/mail/__init__.py`
- Create: `src/harbor_clerk/mail/exceptions.py`
- Create: `src/harbor_clerk/mail/imap_client.py`
- Create: `tests/mail/__init__.py`
- Create: `tests/mail/test_imap_client.py`

- [ ] **Step 1: Add `aioimaplib` to `pyproject.toml`**

In the `[project] dependencies` list, insert (alphabetically, after `aiofiles` if present or after `alembic`):

```toml
    "aioimaplib>=1.1.0",
```

Run `uv sync` to update the lock file.

- [ ] **Step 2: Write the failing test**

```python
# tests/mail/__init__.py — empty file
```

```python
# tests/mail/test_imap_client.py
"""IMAPConnection wraps aioimaplib with typed exceptions."""

import pytest

from harbor_clerk.mail.exceptions import AuthError
from harbor_clerk.mail.imap_client import IMAPConnection


def test_constructor_stores_credentials():
    conn = IMAPConnection(host="imap.example.com", port=993, username="alex@example.com", password="abcd")
    assert conn.host == "imap.example.com"
    assert conn.port == 993
    assert conn.username == "alex@example.com"
    # Password is NOT exposed as a public attribute — only available during login()
    assert not hasattr(conn, "password")


def test_AuthError_is_distinct_exception_type():
    exc = AuthError("invalid credentials")
    assert str(exc) == "invalid credentials"
    assert isinstance(exc, Exception)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_imap_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_clerk.mail'`.

- [ ] **Step 4: Implement the skeleton**

```python
# src/harbor_clerk/mail/__init__.py
"""IMAP sync subsystem.

Headless engine that connects to user-owned IMAP accounts (Gmail, iCloud,
Fastmail, Yahoo, generic IMAP-with-app-password), watches one or more
labels per account, and populates `watched_messages` rows as mail arrives
or changes.

See spec docs/superpowers/specs/2026-05-04-email-ingestion-design.md.
"""

from harbor_clerk.mail.exceptions import (
    AuthError,
    IdleNotSupported,
    UidValidityChanged,
)
from harbor_clerk.mail.imap_client import IMAPConnection

__all__ = ["AuthError", "IMAPConnection", "IdleNotSupported", "UidValidityChanged"]
```

```python
# src/harbor_clerk/mail/exceptions.py
"""Typed exceptions for the mail subsystem.

Distinct types let the sync state machine and the API layer make policy
decisions without parsing error strings.
"""

from __future__ import annotations


class MailError(Exception):
    """Base for all mail subsystem errors."""


class AuthError(MailError):
    """IMAP login failed — wrong password, account locked, or app passwords
    disabled. Sync engine marks the account `status='auth_error'` and stops
    polling until the operator reconnects."""


class IdleNotSupported(MailError):
    """The IMAP server doesn't advertise the IDLE capability. Sync falls
    back to 2-minute polling."""


class UidValidityChanged(MailError):
    """Server-side UIDVALIDITY changed since the last sync. Cursor is
    invalid; trigger a full rescan of the label."""
```

```python
# src/harbor_clerk/mail/imap_client.py
"""Thin async wrapper around aioimaplib.IMAP4_SSL.

Exposes only the operations the sync engine needs (LOGIN, LIST, SELECT,
UID FETCH/SEARCH, IDLE, LOGOUT) and translates aioimaplib's untyped
errors into typed exceptions from `harbor_clerk.mail.exceptions`.

The password is accepted in the constructor but never stored as an
attribute — it lives in a closure during `login()` and goes out of scope
afterwards. This makes accidental logging of the connection object safe.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class IMAPConnection:
    """Connection to one IMAP account.

    Lifecycle: __init__ → connect() → login() → (work) → logout().
    Re-use a single instance across multiple commands; create a new one
    for each (account, label) pair the sync engine watches.
    """

    def __init__(self, *, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        # Password held in a closure, not on `self`, so it doesn't survive
        # past login() and can't leak via repr/logging.
        self._login_callback = lambda c: c.login(username, password)
        self._client: Any | None = None
```

The `login()` / `connect()` / etc. methods come in Task 2.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/mail/test_imap_client.py -v`
Expected: PASS — both tests green.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/harbor_clerk/mail/ tests/mail/__init__.py tests/mail/test_imap_client.py
git commit -m "feat(mail): IMAPConnection skeleton + typed exceptions"
```

---

## Task 2: IMAPConnection connect / login / logout

**Files:**
- Modify: `src/harbor_clerk/mail/imap_client.py`
- Modify: `tests/mail/test_imap_client.py`
- Create: `tests/mail/conftest.py` (mock IMAP fixture)

- [ ] **Step 1: Write the failing test**

Append to `tests/mail/test_imap_client.py`:

```python
import pytest

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.exceptions import AuthError


@pytest.fixture
def mock_aioimap(monkeypatch):
    """Patch aioimaplib.IMAP4_SSL with an in-process fake.

    Returns the FakeIMAP class so individual tests can set its `result`
    attribute to control LOGIN behavior.
    """
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_connect_and_login_success(mock_aioimap):
    mock_aioimap.set_login_response("OK", b"LOGIN completed")
    conn = IMAPConnection(host="imap.example.com", port=993, username="alex@example.com", password="abcd")
    await conn.connect()
    await conn.login()
    await conn.logout()


async def test_login_failure_raises_AuthError(mock_aioimap):
    mock_aioimap.set_login_response("NO", b"AUTHENTICATIONFAILED Invalid credentials")
    conn = IMAPConnection(host="imap.example.com", port=993, username="alex@example.com", password="wrong")
    await conn.connect()
    with pytest.raises(AuthError, match="Invalid credentials"):
        await conn.login()


async def test_logout_is_idempotent(mock_aioimap):
    """Calling logout when not logged in should not raise."""
    conn = IMAPConnection(host="imap.example.com", port=993, username="alex@example.com", password="abcd")
    await conn.logout()  # never connected — should be no-op
    await conn.connect()
    mock_aioimap.set_login_response("OK", b"OK")
    await conn.login()
    await conn.logout()
    await conn.logout()  # already logged out — should be no-op
```

```python
# tests/mail/conftest.py
"""Shared fixtures for mail tests.

The FakeIMAP class is a minimal in-process replacement for
aioimaplib.IMAP4_SSL — it implements just the methods the sync engine
calls and lets tests stage canned responses.
"""

from __future__ import annotations

from typing import Any


class _Response:
    """Mimic aioimaplib's response tuple shape: (result, lines)."""

    def __init__(self, result: str, lines: list[bytes] | bytes):
        self.result = result
        self.lines = lines if isinstance(lines, list) else [lines]


class FakeIMAP:
    """In-process IMAP fake.

    Use class-level setters to stage responses; instance methods consume
    them. Reset via `reset()` between tests if you reuse the fixture.
    """

    _login_response: _Response | None = None
    _list_response: _Response | None = None
    _select_response: _Response | None = None
    _uid_search_response: _Response | None = None
    _uid_fetch_response: _Response | None = None
    _capability_response: _Response | None = None
    _idle_events: list[bytes] = []

    def __init__(self, host: str, port: int = 993, **_kwargs):
        self.host = host
        self.port = port
        self.connected = False
        self.logged_in = False

    @classmethod
    def set_login_response(cls, result: str, line: bytes) -> None:
        cls._login_response = _Response(result, line)

    @classmethod
    def set_list_response(cls, result: str, lines: list[bytes]) -> None:
        cls._list_response = _Response(result, lines)

    @classmethod
    def set_select_response(cls, result: str, lines: list[bytes]) -> None:
        cls._select_response = _Response(result, lines)

    @classmethod
    def set_uid_search_response(cls, result: str, lines: list[bytes]) -> None:
        cls._uid_search_response = _Response(result, lines)

    @classmethod
    def set_uid_fetch_response(cls, result: str, lines: list[bytes]) -> None:
        cls._uid_fetch_response = _Response(result, lines)

    @classmethod
    def set_capability_response(cls, result: str, lines: list[bytes]) -> None:
        cls._capability_response = _Response(result, lines)

    @classmethod
    def set_idle_events(cls, events: list[bytes]) -> None:
        cls._idle_events = events

    @classmethod
    def reset(cls) -> None:
        cls._login_response = None
        cls._list_response = None
        cls._select_response = None
        cls._uid_search_response = None
        cls._uid_fetch_response = None
        cls._capability_response = None
        cls._idle_events = []

    async def wait_hello_from_server(self) -> None:
        self.connected = True

    async def login(self, username: str, password: str):
        resp = self._login_response or _Response("OK", b"OK")
        if resp.result == "OK":
            self.logged_in = True
        return resp.result, resp.lines

    async def logout(self):
        self.logged_in = False
        self.connected = False
        return "OK", [b"BYE"]

    async def list(self, reference: str, mailbox: str):
        resp = self._list_response or _Response("OK", [])
        return resp.result, resp.lines

    async def select(self, mailbox: str):
        resp = self._select_response or _Response("OK", [])
        return resp.result, resp.lines

    async def uid_search(self, criteria: str):
        resp = self._uid_search_response or _Response("OK", [b""])
        return resp.result, resp.lines

    async def uid(self, command: str, *args):
        if command == "FETCH":
            resp = self._uid_fetch_response or _Response("OK", [])
            return resp.result, resp.lines
        return "OK", []

    async def capability(self):
        resp = self._capability_response or _Response("OK", [b"CAPABILITY IMAP4rev1 IDLE"])
        return resp.result, resp.lines

    async def idle_start(self, timeout: float = 0):
        return "OK", []

    async def idle_done(self):
        return "OK", []

    async def wait_server_push(self, timeout: float | None = None) -> bytes:
        if self._idle_events:
            return self._idle_events.pop(0)
        raise TimeoutError("no idle events queued")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/mail/test_imap_client.py -v`
Expected: FAIL — `IMAPConnection` has no `connect()`, `login()`, or `logout()` methods yet.

- [ ] **Step 3: Implement connect / login / logout in `imap_client.py`**

Replace the `imap_client.py` file with this full version:

```python
# src/harbor_clerk/mail/imap_client.py
"""Thin async wrapper around aioimaplib.IMAP4_SSL.

Exposes only the operations the sync engine needs and translates
aioimaplib's untyped errors into typed exceptions from
`harbor_clerk.mail.exceptions`.

The password is accepted in the constructor but never stored as an
attribute — it lives in a closure during `login()` and goes out of scope
afterwards. This makes accidental logging of the connection object safe.
"""

from __future__ import annotations

import logging
from typing import Any

import aioimaplib

from harbor_clerk.mail.exceptions import AuthError

logger = logging.getLogger(__name__)


class IMAPConnection:
    """Connection to one IMAP account.

    Lifecycle: __init__ → connect() → login() → (work) → logout().
    Re-use a single instance across multiple commands; create a new one
    for each (account, label) pair the sync engine watches concurrently.
    """

    def __init__(self, *, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self._password = password  # cleared in login() once consumed
        self._client: Any | None = None
        self._logged_in = False

    async def connect(self) -> None:
        """Open TCP connection and complete the IMAP server greeting."""
        self._client = aioimaplib.IMAP4_SSL(host=self.host, port=self.port)
        await self._client.wait_hello_from_server()

    async def login(self) -> None:
        """Authenticate. Raises AuthError on NO/BAD response.

        After this call the password is cleared from memory.
        """
        if self._client is None:
            raise RuntimeError("login() called before connect()")
        result, lines = await self._client.login(self.username, self._password)
        # Clear password regardless of outcome — never keep failed credentials
        # around on the connection object.
        self._password = ""
        if result != "OK":
            detail = b" ".join(lines).decode(errors="replace")
            raise AuthError(detail or "IMAP LOGIN failed")
        self._logged_in = True

    async def logout(self) -> None:
        """Close the connection. Idempotent — safe to call when not logged in."""
        if self._client is None:
            return
        try:
            if self._logged_in:
                await self._client.logout()
        except Exception as exc:
            logger.debug("logout() raised: %s — ignoring", exc)
        finally:
            self._logged_in = False
            self._client = None

    @property
    def client(self) -> Any:
        """The underlying aioimaplib client. For internal use by the
        sync engine only — exposed so callers can issue UID FETCH etc."""
        if self._client is None:
            raise RuntimeError("connection not established")
        return self._client
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_imap_client.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/imap_client.py tests/mail/test_imap_client.py tests/mail/conftest.py
git commit -m "feat(mail): IMAPConnection connect / login / logout with typed errors"
```

---

## Task 3: Label discovery

**Files:**
- Create: `src/harbor_clerk/mail/labels.py`
- Modify: `src/harbor_clerk/mail/imap_client.py` — add `list_folders()` method
- Modify: `src/harbor_clerk/mail/__init__.py` — export `discover_folders`, `Folder`, `FolderTree`
- Create: `tests/mail/test_labels.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_labels.py
"""Label/folder discovery: IMAP LIST → Folder tree, with system-folder
detection."""

import pytest

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.labels import Folder, discover_folders


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_discover_folders_parses_list_response(mock_aioimap):
    # Standard Gmail-style LIST output: each line is `* LIST (flags) "delim" "name"`.
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_list_response("OK", [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "Clerk"',
        b'(\\HasChildren) "/" "Clerk/Contracts"',
        b'(\\HasNoChildren \\Noselect) "/" "[Gmail]"',
        b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
        b'OK LIST completed',
    ])
    conn = IMAPConnection(host="imap.gmail.com", port=993, username="alex", password="x")
    await conn.connect()
    await conn.login()
    folders = await discover_folders(conn)
    await conn.logout()

    paths = [f.path for f in folders]
    assert "INBOX" in paths
    assert "Clerk" in paths
    assert "Clerk/Contracts" in paths
    assert "[Gmail]/All Mail" in paths

    inbox = next(f for f in folders if f.path == "INBOX")
    assert inbox.is_system is False
    all_mail = next(f for f in folders if f.path == "[Gmail]/All Mail")
    assert all_mail.is_system is True

    # \Noselect folders (just structural parents) should be excluded
    assert not any(f.path == "[Gmail]" for f in folders)


async def test_discover_folders_empty_account(mock_aioimap):
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_list_response("OK", [b"OK LIST completed"])
    conn = IMAPConnection(host="imap.example.com", port=993, username="alex", password="x")
    await conn.connect()
    await conn.login()
    folders = await discover_folders(conn)
    await conn.logout()
    assert folders == []


def test_folder_is_system_detects_inbox():
    """INBOX itself is special-cased — many providers send it without flags."""
    f = Folder(path="INBOX", flags=frozenset(), delimiter="/")
    assert f.is_system is True


def test_folder_is_system_detects_xlist_flags():
    """XLIST/SPECIAL-USE flags identify system folders without [Gmail] prefix."""
    for flag in (r"\All", r"\Sent", r"\Drafts", r"\Trash", r"\Junk", r"\Flagged", r"\Important"):
        f = Folder(path="something", flags=frozenset({flag}), delimiter="/")
        assert f.is_system is True, f"flag {flag!r} should mark folder as system"


def test_folder_is_system_user_label():
    f = Folder(path="Clerk/Contracts", flags=frozenset({r"\HasNoChildren"}), delimiter="/")
    assert f.is_system is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_labels.py -v`
Expected: FAIL — `harbor_clerk.mail.labels` does not exist.

- [ ] **Step 3: Implement `labels.py`**

```python
# src/harbor_clerk/mail/labels.py
"""IMAP LIST parsing and folder/label classification.

A 'label' in Gmail's UI is exposed as an IMAP folder. We use the term
'folder' here to match the IMAP protocol; UI/spec language uses 'label'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harbor_clerk.mail.imap_client import IMAPConnection

# Set of IMAP flags that mark a folder as system-managed (not a user label).
# These come from RFC 6154 (SPECIAL-USE) and Gmail's XLIST extension.
_SYSTEM_FLAGS = frozenset({
    r"\All",
    r"\Sent",
    r"\Drafts",
    r"\Trash",
    r"\Junk",
    r"\Spam",
    r"\Flagged",
    r"\Starred",
    r"\Important",
    r"\Archive",
    r"\Noselect",  # structural-only parents (e.g. "[Gmail]"); also filtered out below
})

# IMAP LIST response line shape: `(flag1 flag2) "delim" "name"`
# The delimiter and name are double-quoted; flags are space-separated.
_LIST_LINE_RE = re.compile(
    rb'^\s*\(([^)]*)\)\s+"([^"]*)"\s+"?([^"]+)"?\s*$',
)


@dataclass(frozen=True)
class Folder:
    path: str
    flags: frozenset[str]
    delimiter: str

    @property
    def is_system(self) -> bool:
        """True if this folder is a system mailbox (INBOX, [Gmail]/*, SPECIAL-USE).

        User labels return False. The wizard greys these with a warning so
        the user has to deliberately pick (e.g.) [Gmail]/All Mail.
        """
        if self.path == "INBOX":
            return True
        if self.flags & (_SYSTEM_FLAGS - {r"\Noselect"}):
            return True
        # [Gmail]/Foo style system folders that don't carry SPECIAL-USE flags
        if self.path.startswith("[Gmail]") or self.path.startswith("[Google Mail]"):
            return True
        return False


def _parse_list_line(line: bytes) -> Folder | None:
    """Parse one IMAP LIST response line into a Folder.

    Returns None if the line is the trailing `OK LIST completed` status,
    or doesn't match the LIST shape. Skips \\Noselect entries (structural
    parents like "[Gmail]" that can't be SELECTed and don't hold mail).
    """
    m = _LIST_LINE_RE.match(line)
    if m is None:
        return None  # status line or unparseable
    flags_blob, delim, name = m.groups()
    flags = frozenset(f.decode("utf-8") for f in flags_blob.split() if f)
    if r"\Noselect" in flags:
        return None
    return Folder(
        path=name.decode("utf-8"),
        flags=flags,
        delimiter=delim.decode("utf-8"),
    )


async def discover_folders(conn: IMAPConnection) -> list[Folder]:
    """Issue `LIST "" "*"` and return the parsed folder list.

    Caller must have already called `conn.connect()` and `conn.login()`.
    """
    result, lines = await conn.client.list('""', '"*"')
    if result != "OK":
        return []
    folders: list[Folder] = []
    for line in lines:
        f = _parse_list_line(line if isinstance(line, bytes) else str(line).encode())
        if f is not None:
            folders.append(f)
    return folders
```

Also add to `src/harbor_clerk/mail/__init__.py`:

```python
from harbor_clerk.mail.labels import Folder, discover_folders

__all__ = [
    "AuthError",
    "Folder",
    "IMAPConnection",
    "IdleNotSupported",
    "UidValidityChanged",
    "discover_folders",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/mail/test_labels.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/labels.py src/harbor_clerk/mail/__init__.py tests/mail/test_labels.py
git commit -m "feat(mail): folder discovery via LIST + system-folder classification"
```

---

## Task 4: Pydantic schemas for mail accounts and watched labels

**Files:**
- Create: `src/harbor_clerk/api/schemas/mail.py`
- Create: `tests/api/test_mail_schemas.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_mail_schemas.py
"""Pydantic schemas for the /api/mail/* surface."""

import pytest
from pydantic import ValidationError

from harbor_clerk.api.schemas.mail import (
    MailAccountCreate,
    MailAccountResponse,
    WatchedLabelCreate,
    WatchedLabelResponse,
)


def test_mail_account_create_validates_required_fields():
    body = MailAccountCreate(
        display_name="My Gmail",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="alex@example.com",
        app_password="abcd-efgh-ijkl-mnop",
    )
    assert body.provider == "gmail"
    assert body.imap_port == 993
    assert body.app_password.get_secret_value() == "abcd-efgh-ijkl-mnop"


def test_mail_account_create_rejects_invalid_provider():
    with pytest.raises(ValidationError, match="provider"):
        MailAccountCreate(
            display_name="x",
            provider="not-a-real-provider",
            imap_host="h",
            imap_port=993,
            imap_username="u",
            app_password="p",
        )


def test_mail_account_create_rejects_invalid_port():
    with pytest.raises(ValidationError):
        MailAccountCreate(
            display_name="x",
            provider="gmail",
            imap_host="h",
            imap_port=70000,  # > 65535
            imap_username="u",
            app_password="p",
        )


def test_mail_account_create_app_password_is_secret():
    body = MailAccountCreate(
        display_name="x",
        provider="gmail",
        imap_host="h",
        imap_port=993,
        imap_username="u",
        app_password="secret",
    )
    # repr() should not leak the password
    assert "secret" not in repr(body)


def test_mail_account_response_omits_password():
    """Response schema must never include the app password."""
    fields = set(MailAccountResponse.model_fields.keys())
    assert "app_password" not in fields
    assert "app_password_ciphertext" not in fields


def test_watched_label_create_requires_account_and_path():
    body = WatchedLabelCreate(
        account_id="11111111-1111-1111-1111-111111111111",
        label_path="Clerk",
        display_name="Clerk",
    )
    assert body.label_path == "Clerk"


def test_watched_label_response_includes_status():
    fields = set(WatchedLabelResponse.model_fields.keys())
    assert "status" in fields
    assert "last_synced_at" in fields
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_mail_schemas.py -v`
Expected: FAIL — `harbor_clerk.api.schemas.mail` does not exist.

- [ ] **Step 3: Implement schemas**

```python
# src/harbor_clerk/api/schemas/mail.py
"""Pydantic schemas for /api/mail/* endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

ProviderName = Literal["gmail", "icloud", "fastmail", "yahoo", "generic"]
AccountStatus = Literal["active", "auth_error", "key_mismatch", "paused"]
LabelStatus = Literal["active", "paused", "error"]


class MailAccountCreate(BaseModel):
    """Body for `POST /api/mail/accounts`. The app password is encrypted
    with the process-wide Cipher before being stored; the plaintext lives
    only in this request object and the in-memory IMAPConnection used for
    the test-connection probe."""

    display_name: str = Field(min_length=1, max_length=200)
    provider: ProviderName
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(ge=1, le=65535)
    imap_username: str = Field(min_length=1, max_length=255)
    app_password: SecretStr = Field(min_length=1, max_length=500)


class MailAccountResponse(BaseModel):
    """Response shape for `GET /api/mail/accounts` and individual fetches.
    Never includes the password — neither plaintext nor ciphertext."""

    account_id: UUID
    display_name: str
    provider: ProviderName
    imap_host: str
    imap_port: int
    imap_username: str
    status: AccountStatus
    last_error: str | None
    last_connected_at: datetime | None
    created_at: datetime


class TestConnectionResponse(BaseModel):
    """Response from `POST /api/mail/accounts/{id}/test`. Indicates whether
    the credentials work and, if so, returns the discovered folder list
    so the wizard can render a label picker without a second API call."""

    success: bool
    error: str | None = None
    folders: list["FolderInfo"] = Field(default_factory=list)


class FolderInfo(BaseModel):
    """One folder/label entry as discovered via LIST."""

    path: str
    display_name: str
    is_system: bool
    has_children: bool


class WatchedLabelCreate(BaseModel):
    """Body for `POST /api/mail/labels`."""

    account_id: UUID
    label_path: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=200)


class WatchedLabelResponse(BaseModel):
    label_id: UUID
    account_id: UUID
    label_path: str
    display_name: str
    status: LabelStatus
    last_error: str | None
    last_synced_at: datetime | None
    last_uid_seen: int
    uidvalidity: int | None
    created_at: datetime


# Resolve forward references for nested models
TestConnectionResponse.model_rebuild()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_mail_schemas.py -v`
Expected: PASS — all 7 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/schemas/mail.py tests/api/test_mail_schemas.py
git commit -m "feat(api): Pydantic schemas for /api/mail/* — accounts + labels"
```

---

## Task 5: Mail account CRUD endpoints

**Files:**
- Create: `src/harbor_clerk/api/routes/mail.py`
- Modify: `src/harbor_clerk/api/app.py` — register the router
- Create: `tests/api/test_mail_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_mail_routes.py
"""Tests for /api/mail/* CRUD endpoints."""

import base64
import os
from uuid import UUID

import pytest
from sqlalchemy import select

from harbor_clerk.models import MailAccount
from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def setup_master_key(monkeypatch):
    monkeypatch.setenv(
        "HARBOR_CLERK_MASTER_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    from harbor_clerk.secrets import _accessor
    _accessor.reset()
    yield
    _accessor.reset()


async def test_create_mail_account_admin(client, admin_user, admin_token, db_session):
    body = {
        "display_name": "My Gmail",
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "alex@example.com",
        "app_password": "abcd-efgh-ijkl-mnop",
    }
    resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "account_id" in data
    assert "app_password" not in data
    assert "app_password_ciphertext" not in data
    assert data["status"] == "active"
    assert data["imap_username"] == "alex@example.com"

    # Verify the row exists with encrypted password (not plaintext)
    account_id = UUID(data["account_id"])
    fetched = (
        await db_session.execute(select(MailAccount).where(MailAccount.account_id == account_id))
    ).scalar_one()
    assert fetched.app_password_ciphertext != b"abcd-efgh-ijkl-mnop"
    assert len(fetched.key_fingerprint) == 8


async def test_create_mail_account_requires_admin(client, regular_user, regular_token):
    body = {
        "display_name": "x", "provider": "gmail", "imap_host": "h", "imap_port": 993,
        "imap_username": "u", "app_password": "p",
    }
    resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(regular_token))
    assert resp.status_code == 403


async def test_create_mail_account_duplicate_returns_409(client, admin_user, admin_token):
    body = {
        "display_name": "first", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "dup@example.com", "app_password": "x",
    }
    r1 = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    assert r1.status_code == 201
    body["display_name"] = "second"
    r2 = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    assert r2.status_code == 409


async def test_list_mail_accounts(client, admin_user, admin_token, db_session):
    body = {
        "display_name": "list-test", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "list@example.com", "app_password": "x",
    }
    await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))

    resp = await client.get("/api/mail/accounts", headers=auth_header(admin_token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    account = next(a for a in items if a["imap_username"] == "list@example.com")
    assert "app_password" not in account


async def test_delete_mail_account_cascades(client, admin_user, admin_token, db_session):
    body = {
        "display_name": "del-test", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "del@example.com", "app_password": "x",
    }
    create_resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create_resp.json()["account_id"]

    del_resp = await client.delete(f"/api/mail/accounts/{account_id}", headers=auth_header(admin_token))
    assert del_resp.status_code == 204

    # Verify gone
    list_resp = await client.get("/api/mail/accounts", headers=auth_header(admin_token))
    usernames = {a["imap_username"] for a in list_resp.json()}
    assert "del@example.com" not in usernames
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_mail_routes.py::test_create_mail_account_admin -v`
Expected: FAIL — `404 Not Found` (the `/api/mail/accounts` route doesn't exist yet).

- [ ] **Step 3: Implement the router**

```python
# src/harbor_clerk/api/routes/mail.py
"""REST endpoints for /api/mail/* — admin-only.

Stage 2 surface: account CRUD + connection-test, watched-label CRUD +
manual rescan. Used by the API layer (humans via auth) and by Stage 4's UI.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.mail import (
    MailAccountCreate,
    MailAccountResponse,
)
from harbor_clerk.db import get_session
from harbor_clerk.models import MailAccount
from harbor_clerk.secrets import get_cipher

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mail", tags=["mail"])


def _account_to_response(a: MailAccount) -> MailAccountResponse:
    return MailAccountResponse(
        account_id=a.account_id,
        display_name=a.display_name,
        provider=a.provider,  # type: ignore[arg-type]
        imap_host=a.imap_host,
        imap_port=a.imap_port,
        imap_username=a.imap_username,
        status=a.status,  # type: ignore[arg-type]
        last_error=a.last_error,
        last_connected_at=a.last_connected_at,
        created_at=a.created_at,
    )


@router.post(
    "/accounts",
    response_model=MailAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_mail_account(
    body: MailAccountCreate,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> MailAccountResponse:
    """Create a mail account. Encrypts the app password via the
    process-wide Cipher before persisting. Does NOT test the connection
    here — call `POST /api/mail/accounts/{id}/test` separately."""
    cipher = get_cipher()
    ciphertext, fingerprint = cipher.encrypt(body.app_password.get_secret_value().encode())

    account = MailAccount(
        display_name=body.display_name,
        provider=body.provider,
        imap_host=body.imap_host,
        imap_port=body.imap_port,
        imap_username=body.imap_username,
        app_password_ciphertext=ciphertext,
        key_fingerprint=fingerprint,
    )
    session.add(account)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"mail account ({body.imap_host}, {body.imap_username}) already exists",
        )
    await session.commit()
    return _account_to_response(account)


@router.get("/accounts", response_model=list[MailAccountResponse])
async def list_mail_accounts(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[MailAccountResponse]:
    """List all mail accounts. Never returns secrets."""
    rows = (
        await session.execute(select(MailAccount).order_by(MailAccount.created_at))
    ).scalars().all()
    return [_account_to_response(a) for a in rows]


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mail_account(
    account_id: UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a mail account. Cascades to watched_labels and watched_messages
    (Documents created from those messages are NOT deleted — they remain in
    the corpus). The Stage 2 sync engine will react to the LISTEN/NOTIFY
    and stop polling immediately."""
    account = (
        await session.execute(select(MailAccount).where(MailAccount.account_id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mail account not found")
    await session.delete(account)
    await session.commit()
```

Then register the router in `src/harbor_clerk/api/app.py`. Find the existing `app.include_router(...)` calls and add:

```python
from harbor_clerk.api.routes import mail as mail_routes

app.include_router(mail_routes.router)
```

(Match the pattern of the existing route registrations.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_mail_routes.py -v -k "create or list or delete"`
Expected: PASS — 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/mail.py src/harbor_clerk/api/app.py tests/api/test_mail_routes.py
git commit -m "feat(api): mail account CRUD — POST/GET/DELETE /api/mail/accounts"
```

---

## Task 6: Test-connection + label discovery endpoints

**Files:**
- Modify: `src/harbor_clerk/api/routes/mail.py`
- Modify: `tests/api/test_mail_routes.py`

The test-connection endpoint actually opens an IMAP connection using the stored credentials, runs LIST, and returns the folder list. This is what the Stage 4 wizard will use to populate its label picker.

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_mail_routes.py`:

```python
async def test_test_connection_endpoint(client, admin_user, admin_token, monkeypatch):
    """Test connection endpoint: opens IMAP conn, runs LIST, returns folders."""
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    FakeIMAP.set_login_response("OK", b"OK")
    FakeIMAP.set_list_response("OK", [
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "Clerk"',
        b'OK LIST completed',
    ])
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)

    body = {
        "display_name": "test-conn", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "tc@example.com", "app_password": "good",
    }
    create_resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create_resp.json()["account_id"]

    test_resp = await client.post(
        f"/api/mail/accounts/{account_id}/test",
        headers=auth_header(admin_token),
    )
    assert test_resp.status_code == 200
    data = test_resp.json()
    assert data["success"] is True
    paths = [f["path"] for f in data["folders"]]
    assert "INBOX" in paths
    assert "Clerk" in paths


async def test_test_connection_auth_error(client, admin_user, admin_token, monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    FakeIMAP.set_login_response("NO", b"AUTHENTICATIONFAILED Invalid credentials")
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)

    body = {
        "display_name": "auth-fail", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "fail@example.com", "app_password": "wrong",
    }
    create_resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create_resp.json()["account_id"]

    test_resp = await client.post(
        f"/api/mail/accounts/{account_id}/test",
        headers=auth_header(admin_token),
    )
    assert test_resp.status_code == 200
    data = test_resp.json()
    assert data["success"] is False
    assert "AUTHENTICATIONFAILED" in data["error"]
    assert data["folders"] == []

    # Account status should now be 'auth_error'
    list_resp = await client.get("/api/mail/accounts", headers=auth_header(admin_token))
    a = next(x for x in list_resp.json() if x["imap_username"] == "fail@example.com")
    assert a["status"] == "auth_error"
    assert "AUTHENTICATIONFAILED" in a["last_error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_mail_routes.py::test_test_connection_endpoint -v`
Expected: FAIL — `404 Not Found`.

- [ ] **Step 3: Implement the test-connection endpoint**

Append to `src/harbor_clerk/api/routes/mail.py`:

```python
from datetime import UTC, datetime

from harbor_clerk.api.schemas.mail import FolderInfo, TestConnectionResponse
from harbor_clerk.mail import (
    AuthError,
    IMAPConnection,
    discover_folders,
)


@router.post(
    "/accounts/{account_id}/test",
    response_model=TestConnectionResponse,
)
async def test_mail_account(
    account_id: UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> TestConnectionResponse:
    """Open an IMAP connection with the stored credentials and run LIST.

    Returns the discovered folder list on success. On auth failure,
    updates the account row to status='auth_error' so the UI knows
    re-entry is needed.
    """
    account = (
        await session.execute(select(MailAccount).where(MailAccount.account_id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mail account not found")

    cipher = get_cipher()
    try:
        password = cipher.decrypt(account.app_password_ciphertext, account.key_fingerprint).decode()
    except Exception as exc:
        logger.warning("decrypt failed for account %s: %s", account_id, exc)
        account.status = "key_mismatch"
        account.last_error = "key fingerprint does not match active master key"
        await session.commit()
        return TestConnectionResponse(success=False, error=account.last_error)

    conn = IMAPConnection(
        host=account.imap_host,
        port=account.imap_port,
        username=account.imap_username,
        password=password,
    )
    try:
        await conn.connect()
        await conn.login()
        folders = await discover_folders(conn)
        await conn.logout()
    except AuthError as exc:
        account.status = "auth_error"
        account.last_error = str(exc)
        await session.commit()
        return TestConnectionResponse(success=False, error=str(exc))
    except Exception as exc:
        # Network errors, timeouts, etc. — don't change account status
        # (the account isn't broken, just unreachable right now).
        try:
            await conn.logout()
        except Exception:
            pass
        return TestConnectionResponse(success=False, error=f"connection failed: {exc}")

    account.status = "active"
    account.last_error = None
    account.last_connected_at = datetime.now(UTC)
    await session.commit()

    return TestConnectionResponse(
        success=True,
        folders=[
            FolderInfo(
                path=f.path,
                display_name=f.path,
                is_system=f.is_system,
                has_children=r"\HasChildren" in f.flags,
            )
            for f in folders
        ],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_mail_routes.py -v -k "test_connection"`
Expected: PASS — 2 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/mail.py tests/api/test_mail_routes.py
git commit -m "feat(api): POST /api/mail/accounts/{id}/test — verify creds + return folders"
```

---

## Task 7: Watched label CRUD endpoints

**Files:**
- Modify: `src/harbor_clerk/api/routes/mail.py`
- Modify: `tests/api/test_mail_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_mail_routes.py`:

```python
async def test_create_watched_label(client, admin_user, admin_token):
    body = {
        "display_name": "label-test", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "labels@example.com", "app_password": "x",
    }
    create = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create.json()["account_id"]

    label_body = {"account_id": account_id, "label_path": "Clerk", "display_name": "Clerk"}
    resp = await client.post("/api/mail/labels", json=label_body, headers=auth_header(admin_token))
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["label_path"] == "Clerk"
    assert data["status"] == "active"
    assert data["last_uid_seen"] == 0
    assert data["uidvalidity"] is None  # not yet synced


async def test_create_watched_label_duplicate_returns_409(client, admin_user, admin_token):
    body = {
        "display_name": "dup-label", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "duplab@example.com", "app_password": "x",
    }
    create = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create.json()["account_id"]

    label_body = {"account_id": account_id, "label_path": "Same", "display_name": "Same"}
    r1 = await client.post("/api/mail/labels", json=label_body, headers=auth_header(admin_token))
    assert r1.status_code == 201
    r2 = await client.post("/api/mail/labels", json=label_body, headers=auth_header(admin_token))
    assert r2.status_code == 409


async def test_create_watched_label_unknown_account_404(client, admin_user, admin_token):
    label_body = {
        "account_id": "00000000-0000-0000-0000-000000000000",
        "label_path": "Clerk",
        "display_name": "Clerk",
    }
    resp = await client.post("/api/mail/labels", json=label_body, headers=auth_header(admin_token))
    assert resp.status_code == 404


async def test_list_watched_labels(client, admin_user, admin_token):
    body = {
        "display_name": "list-labels", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "lstlab@example.com", "app_password": "x",
    }
    create = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create.json()["account_id"]
    await client.post(
        "/api/mail/labels",
        json={"account_id": account_id, "label_path": "L1", "display_name": "L1"},
        headers=auth_header(admin_token),
    )
    await client.post(
        "/api/mail/labels",
        json={"account_id": account_id, "label_path": "L2", "display_name": "L2"},
        headers=auth_header(admin_token),
    )

    resp = await client.get("/api/mail/labels", headers=auth_header(admin_token))
    assert resp.status_code == 200
    items = resp.json()
    paths = [it["label_path"] for it in items if it["account_id"] == account_id]
    assert "L1" in paths
    assert "L2" in paths


async def test_delete_watched_label(client, admin_user, admin_token):
    body = {
        "display_name": "del-label", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "dellab@example.com", "app_password": "x",
    }
    create = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create.json()["account_id"]
    label_resp = await client.post(
        "/api/mail/labels",
        json={"account_id": account_id, "label_path": "ToDelete", "display_name": "ToDelete"},
        headers=auth_header(admin_token),
    )
    label_id = label_resp.json()["label_id"]

    resp = await client.delete(f"/api/mail/labels/{label_id}", headers=auth_header(admin_token))
    assert resp.status_code == 204

    list_resp = await client.get("/api/mail/labels", headers=auth_header(admin_token))
    assert not any(it["label_id"] == label_id for it in list_resp.json())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_mail_routes.py -v -k "label"`
Expected: FAIL — label routes don't exist.

- [ ] **Step 3: Implement label CRUD endpoints**

Append to `src/harbor_clerk/api/routes/mail.py`:

```python
from harbor_clerk.api.schemas.mail import WatchedLabelCreate, WatchedLabelResponse
from harbor_clerk.models import WatchedLabel


def _label_to_response(lbl: WatchedLabel) -> WatchedLabelResponse:
    return WatchedLabelResponse(
        label_id=lbl.label_id,
        account_id=lbl.account_id,
        label_path=lbl.label_path,
        display_name=lbl.display_name,
        status=lbl.status,  # type: ignore[arg-type]
        last_error=lbl.last_error,
        last_synced_at=lbl.last_synced_at,
        last_uid_seen=lbl.last_uid_seen,
        uidvalidity=lbl.uidvalidity,
        created_at=lbl.created_at,
    )


@router.post(
    "/labels",
    response_model=WatchedLabelResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_watched_label(
    body: WatchedLabelCreate,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> WatchedLabelResponse:
    """Add a watched label to an account. The sync engine picks it up
    via LISTEN/NOTIFY and starts a sync task on the next supervisor tick."""
    account = (
        await session.execute(select(MailAccount).where(MailAccount.account_id == body.account_id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mail account not found")

    label = WatchedLabel(
        account_id=body.account_id,
        label_path=body.label_path,
        display_name=body.display_name,
    )
    session.add(label)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"label '{body.label_path}' already watched on this account",
        )
    await session.commit()
    return _label_to_response(label)


@router.get("/labels", response_model=list[WatchedLabelResponse])
async def list_watched_labels(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[WatchedLabelResponse]:
    rows = (
        await session.execute(select(WatchedLabel).order_by(WatchedLabel.created_at))
    ).scalars().all()
    return [_label_to_response(lbl) for lbl in rows]


@router.delete("/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watched_label(
    label_id: UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    label = (
        await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))
    ).scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watched label not found")
    await session.delete(label)
    await session.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/api/test_mail_routes.py -v -k "label"`
Expected: PASS — all 5 label tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/mail.py tests/api/test_mail_routes.py
git commit -m "feat(api): watched_label CRUD — POST/GET/DELETE /api/mail/labels"
```

---

## Task 8: Per-label cursor helpers

**Files:**
- Create: `src/harbor_clerk/mail/cursor.py`
- Create: `tests/mail/test_cursor.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_cursor.py
"""Cursor read/write helpers for per-label sync state."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from harbor_clerk.mail.cursor import LabelCursor, read_cursor, write_cursor
from harbor_clerk.models import MailAccount, WatchedLabel


@pytest.fixture
async def label(db_session) -> WatchedLabel:
    account = MailAccount(
        display_name="cursor-test",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username=f"cursor-{uuid4()}@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()
    lbl = WatchedLabel(
        account_id=account.account_id,
        label_path="Cursor",
        display_name="Cursor",
    )
    db_session.add(lbl)
    await db_session.flush()
    return lbl


async def test_read_cursor_empty(db_session, label):
    """A freshly-created label has cursor (last_uid_seen=0, uidvalidity=None)."""
    cursor = await read_cursor(db_session, label.label_id)
    assert cursor.last_uid_seen == 0
    assert cursor.uidvalidity is None


async def test_write_cursor_updates_label_row(db_session, label):
    new = LabelCursor(last_uid_seen=42, uidvalidity=12345)
    await write_cursor(db_session, label.label_id, new)
    await db_session.commit()

    refetched = await read_cursor(db_session, label.label_id)
    assert refetched.last_uid_seen == 42
    assert refetched.uidvalidity == 12345


async def test_write_cursor_advances_only(db_session, label):
    """write_cursor never moves last_uid_seen backwards within the same uidvalidity."""
    await write_cursor(db_session, label.label_id, LabelCursor(last_uid_seen=100, uidvalidity=12345))
    await db_session.commit()

    # Try to move backwards
    with pytest.raises(ValueError, match="cursor cannot move backwards"):
        await write_cursor(
            db_session,
            label.label_id,
            LabelCursor(last_uid_seen=50, uidvalidity=12345),
        )


async def test_write_cursor_resets_on_uidvalidity_change(db_session, label):
    """A different uidvalidity is a new epoch — last_uid_seen restarts."""
    await write_cursor(db_session, label.label_id, LabelCursor(last_uid_seen=100, uidvalidity=12345))
    await db_session.commit()

    # New uidvalidity → cursor resets
    await write_cursor(db_session, label.label_id, LabelCursor(last_uid_seen=5, uidvalidity=99999))
    await db_session.commit()

    refetched = await read_cursor(db_session, label.label_id)
    assert refetched.uidvalidity == 99999
    assert refetched.last_uid_seen == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_cursor.py -v`
Expected: FAIL — `harbor_clerk.mail.cursor` doesn't exist.

- [ ] **Step 3: Implement `cursor.py`**

```python
# src/harbor_clerk/mail/cursor.py
"""Per-label cursor read/write helpers.

The cursor (`uidvalidity`, `last_uid_seen`) lives on the WatchedLabel row.
This module wraps the read/write so the sync engine doesn't need to issue
raw SQL — and centralizes the invariant that within a single uidvalidity
epoch the cursor only advances.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import WatchedLabel


@dataclass(frozen=True)
class LabelCursor:
    """Position within a label's IMAP UID stream."""

    last_uid_seen: int
    uidvalidity: int | None


async def read_cursor(session: AsyncSession, label_id: UUID) -> LabelCursor:
    """Read the cursor from the label row."""
    label = (
        await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))
    ).scalar_one()
    return LabelCursor(
        last_uid_seen=label.last_uid_seen,
        uidvalidity=label.uidvalidity,
    )


async def write_cursor(
    session: AsyncSession,
    label_id: UUID,
    new: LabelCursor,
) -> None:
    """Write the cursor to the label row.

    Within the same `uidvalidity`, refuses to move `last_uid_seen` backwards.
    A new `uidvalidity` is treated as a fresh epoch — any value is accepted.
    """
    label = (
        await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))
    ).scalar_one()

    if label.uidvalidity == new.uidvalidity and new.last_uid_seen < label.last_uid_seen:
        raise ValueError(
            f"cursor cannot move backwards within uidvalidity={new.uidvalidity}: "
            f"current={label.last_uid_seen}, attempted={new.last_uid_seen}"
        )

    label.uidvalidity = new.uidvalidity
    label.last_uid_seen = new.last_uid_seen
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_cursor.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/cursor.py tests/mail/test_cursor.py
git commit -m "feat(mail): per-label cursor helpers (last_uid_seen, uidvalidity)"
```

---

## Task 9: Initial sync — empty cursor → fetch all UIDs → populate watched_messages

**Files:**
- Create: `src/harbor_clerk/mail/sync.py`
- Modify: `src/harbor_clerk/mail/__init__.py` — export `sync_label_initial`
- Modify: `tests/mail/conftest.py` — add fixture for an account+label
- Create: `tests/mail/test_sync_initial.py`

- [ ] **Step 1: Add account+label fixtures**

Append to `tests/mail/conftest.py`:

```python
import pytest
from uuid import uuid4

from harbor_clerk.models import MailAccount, WatchedLabel


@pytest.fixture
async def mail_account(db_session) -> MailAccount:
    account = MailAccount(
        display_name="sync-test",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username=f"sync-{uuid4()}@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()
    return account


@pytest.fixture
async def watched_label(db_session, mail_account) -> WatchedLabel:
    lbl = WatchedLabel(
        account_id=mail_account.account_id,
        label_path="Sync",
        display_name="Sync",
    )
    db_session.add(lbl)
    await db_session.flush()
    return lbl
```

- [ ] **Step 2: Write the failing test**

```python
# tests/mail/test_sync_initial.py
"""Initial sync: empty cursor → fetch all UIDs in label → populate
watched_messages rows."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import sync_label_initial
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_initial_sync_empty_label(db_session, watched_label, mock_aioimap):
    """An empty IMAP label produces no watched_messages rows but still
    establishes the cursor (uidvalidity, last_uid_seen=0)."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 0 EXISTS",
        b"* 0 RECENT",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"* OK [UIDNEXT 1] Predicted next UID",
        b"OK [READ-ONLY] SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(
        host="imap.example.com", port=993, username="x", password="y",
    )
    await conn.connect()
    await conn.login()
    summary = await sync_label_initial(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 0
    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)
    )).scalars().all()
    assert len(rows) == 0

    # Cursor should be set
    await db_session.refresh(watched_label)
    assert watched_label.uidvalidity == 12345
    assert watched_label.last_uid_seen == 0


async def test_initial_sync_with_three_messages(db_session, watched_label, mock_aioimap):
    """Three messages in the label → three watched_messages rows; cursor
    advances to highest UID."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 3 EXISTS",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b"1 2 3"])
    # FETCH returns one envelope-stub per message — for initial sync we
    # only need the UID and Message-ID header.
    mock_aioimap.set_uid_fetch_response("OK", [
        b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <msg1@example.com>\r\n",
        b")",
        b"2 (UID 2 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <msg2@example.com>\r\n",
        b")",
        b"3 (UID 3 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <msg3@example.com>\r\n",
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await sync_label_initial(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 3
    rows = (await db_session.execute(
        select(WatchedMessage)
        .where(WatchedMessage.label_id == watched_label.label_id)
        .order_by(WatchedMessage.imap_uid)
    )).scalars().all()
    assert len(rows) == 3
    assert [r.imap_uid for r in rows] == [1, 2, 3]
    assert [r.message_id for r in rows] == [
        "<msg1@example.com>",
        "<msg2@example.com>",
        "<msg3@example.com>",
    ]
    assert all(r.status == "active" for r in rows)

    await db_session.refresh(watched_label)
    assert watched_label.uidvalidity == 12345
    assert watched_label.last_uid_seen == 3


async def test_initial_sync_idempotent_on_re_run(db_session, watched_label, mock_aioimap):
    """Running initial sync twice doesn't duplicate rows — UNIQUE
    constraint on (label_id, message_id) plus the eml_sha dedup catches
    repeats."""
    # First run
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 1 EXISTS", b"* OK [UIDVALIDITY 12345] UIDs valid", b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b"1"])
    mock_aioimap.set_uid_fetch_response("OK", [
        b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <only@example.com>\r\n",
        b")",
        b"OK FETCH completed",
    ])
    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    await sync_label_initial(db_session, conn, watched_label)
    await db_session.commit()
    await conn.logout()

    # Second run with same data
    conn2 = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn2.connect()
    await conn2.login()
    await sync_label_initial(db_session, conn2, watched_label)
    await db_session.commit()
    await conn2.logout()

    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)
    )).scalars().all()
    assert len(rows) == 1
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_sync_initial.py -v`
Expected: FAIL — `harbor_clerk.mail.sync` doesn't exist.

- [ ] **Step 4: Implement `sync.py`**

```python
# src/harbor_clerk/mail/sync.py
"""Per-label sync state machine.

Stage 2 implements:
  - sync_label_initial: empty cursor → fetch all UIDs → populate watched_messages
  - sync_label_incremental: fetch UIDs > last_uid_seen → append to watched_messages
  - check_uidvalidity: detect server-side UIDVALIDITY change → trigger rescan

Documents are NOT yet created from these messages — that's Stage 3, which
will read freshly-inserted watched_messages rows and produce email +
attachment Documents via the parser.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.cursor import LabelCursor, write_cursor
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.models import WatchedLabel, WatchedMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncSummary:
    """What a sync invocation did."""

    fetched_count: int
    new_count: int
    duplicate_count: int


_MESSAGE_ID_RE = re.compile(rb"Message-I[Dd]:\s*(<[^>]+>)", re.IGNORECASE)


def _parse_uidvalidity(select_lines: list[bytes]) -> int | None:
    """Extract UIDVALIDITY from a SELECT response. Format:
    `* OK [UIDVALIDITY 12345] UIDs valid`."""
    for line in select_lines:
        m = re.search(rb"UIDVALIDITY\s+(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def _parse_uid_list(search_lines: list[bytes]) -> list[int]:
    """Parse `* SEARCH 1 2 3` style output. The aioimaplib `uid_search`
    helper returns just the UID list as bytes, possibly empty."""
    uids: list[int] = []
    for line in search_lines:
        for tok in line.split():
            try:
                uids.append(int(tok))
            except ValueError:
                continue
    return uids


def _parse_fetch_response(fetch_lines: list[bytes]) -> dict[int, str]:
    """From the FETCH response, build {uid: message_id}.

    The response is multi-line with literal headers. We look for `UID N`
    in the structural lines and `Message-ID: <...>` in the header lines.
    Falls back to a synthesized hash when the Message-ID header is absent.
    """
    result: dict[int, str] = {}
    current_uid: int | None = None
    for line in fetch_lines:
        # Structural line: `1 (UID 1 BODY[HEADER...]`
        m = re.match(rb"^\s*\d+\s+\(UID\s+(\d+)", line)
        if m:
            current_uid = int(m.group(1))
            continue
        # Message-ID header line
        if current_uid is not None:
            mid_match = _MESSAGE_ID_RE.search(line)
            if mid_match:
                result[current_uid] = mid_match.group(1).decode("utf-8", errors="replace")
                # Don't reset current_uid — multi-line literals may continue
    return result


def _synthesize_message_id(uid: int, label_id) -> str:
    """Fallback Message-ID for messages with no header. Stable per-(label, uid)."""
    h = hashlib.sha256(f"{label_id}:{uid}".encode()).hexdigest()[:16]
    return f"<synthetic-{h}@harborclerk.local>"


async def sync_label_initial(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> SyncSummary:
    """Fetch all messages currently in the label and populate watched_messages.

    Caller must have already opened and authenticated `conn`. This function
    only reads from IMAP and writes to Postgres — no commit. Caller commits.
    """
    select_result, select_lines = await conn.client.select(label.label_path)
    if select_result != "OK":
        logger.warning("SELECT %r failed: %r", label.label_path, select_lines)
        return SyncSummary(fetched_count=0, new_count=0, duplicate_count=0)

    uidvalidity = _parse_uidvalidity(select_lines)

    # Find all UIDs in the label
    search_result, search_lines = await conn.client.uid_search("ALL")
    if search_result != "OK":
        return SyncSummary(0, 0, 0)
    uids = _parse_uid_list(search_lines)
    if not uids:
        await write_cursor(session, label.label_id, LabelCursor(last_uid_seen=0, uidvalidity=uidvalidity))
        return SyncSummary(0, 0, 0)

    # Fetch Message-ID for each
    uid_set = ",".join(str(u) for u in uids)
    fetch_result, fetch_lines = await conn.client.uid(
        "FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
    )
    if fetch_result != "OK":
        return SyncSummary(0, 0, 0)
    uid_to_mid = _parse_fetch_response(fetch_lines)

    # Insert watched_messages rows. eml_sha256 is a placeholder here
    # (Stage 3 fills it when the .eml is fetched and parsed). Status is 'active'.
    new_count = 0
    duplicate_count = 0
    for uid in uids:
        message_id = uid_to_mid.get(uid) or _synthesize_message_id(uid, label.label_id)
        # Dedup check: same (label_id, message_id) means we've seen this before.
        existing = (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            duplicate_count += 1
            continue
        # Placeholder eml_sha256 — Stage 3 fills with the actual SHA when fetching the .eml.
        placeholder_sha = hashlib.sha256(f"placeholder:{label.label_id}:{uid}".encode()).digest()
        msg = WatchedMessage(
            label_id=label.label_id,
            message_id=message_id,
            imap_uid=uid,
            eml_sha256=placeholder_sha,
            status="active",
        )
        session.add(msg)
        new_count += 1

    await session.flush()

    # Advance cursor to highest UID seen
    highest_uid = max(uids)
    await write_cursor(
        session, label.label_id, LabelCursor(last_uid_seen=highest_uid, uidvalidity=uidvalidity)
    )

    return SyncSummary(fetched_count=len(uids), new_count=new_count, duplicate_count=duplicate_count)
```

Add to `src/harbor_clerk/mail/__init__.py`:

```python
from harbor_clerk.mail.sync import SyncSummary, sync_label_initial

__all__ = [
    # ... existing ...
    "SyncSummary",
    "sync_label_initial",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_sync_initial.py -v`
Expected: PASS — all 3 tests green.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mail/sync.py src/harbor_clerk/mail/__init__.py tests/mail/conftest.py tests/mail/test_sync_initial.py
git commit -m "feat(mail): initial sync — fetch all UIDs in label, populate watched_messages"
```

---

## Task 10: Incremental sync — fetch UIDs > last_uid_seen

**Files:**
- Modify: `src/harbor_clerk/mail/sync.py`
- Modify: `src/harbor_clerk/mail/__init__.py`
- Create: `tests/mail/test_sync_incremental.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_sync_incremental.py
"""Incremental sync: cursor advances on new messages, ignores already-seen ones."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import sync_label_incremental
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_incremental_sync_fetches_only_new_uids(db_session, watched_label, mock_aioimap):
    # Set existing cursor: we've seen up to UID 5
    watched_label.last_uid_seen = 5
    watched_label.uidvalidity = 12345
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 7 EXISTS",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"OK SELECT completed",
    ])
    # Server now has UIDs 1-7; the search query `UID 6:*` returns only 6 and 7.
    mock_aioimap.set_uid_search_response("OK", [b"6 7"])
    mock_aioimap.set_uid_fetch_response("OK", [
        b"6 (UID 6 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <new1@example.com>\r\n",
        b")",
        b"7 (UID 7 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <new2@example.com>\r\n",
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await sync_label_incremental(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert summary.fetched_count == 2
    assert summary.new_count == 2

    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)
        .order_by(WatchedMessage.imap_uid)
    )).scalars().all()
    assert [r.imap_uid for r in rows] == [6, 7]

    await db_session.refresh(watched_label)
    assert watched_label.last_uid_seen == 7


async def test_incremental_sync_no_new_messages(db_session, watched_label, mock_aioimap):
    """If `UID last+1:*` returns empty, sync is a no-op."""
    watched_label.last_uid_seen = 100
    watched_label.uidvalidity = 12345
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 5 EXISTS",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await sync_label_incremental(db_session, conn, watched_label)
    await conn.logout()

    assert summary.fetched_count == 0
    assert summary.new_count == 0

    await db_session.refresh(watched_label)
    assert watched_label.last_uid_seen == 100  # unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_sync_incremental.py -v`
Expected: FAIL — `sync_label_incremental` doesn't exist.

- [ ] **Step 3: Implement `sync_label_incremental`**

Append to `src/harbor_clerk/mail/sync.py`:

```python
async def sync_label_incremental(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> SyncSummary:
    """Fetch messages with UID > last_uid_seen and append to watched_messages.

    Caller must have already authenticated. Caller commits.
    """
    select_result, select_lines = await conn.client.select(label.label_path)
    if select_result != "OK":
        return SyncSummary(0, 0, 0)

    uidvalidity = _parse_uidvalidity(select_lines)
    if label.uidvalidity is not None and uidvalidity != label.uidvalidity:
        # UIDVALIDITY changed — caller must trigger a full rescan instead.
        # We refuse to advance the cursor in this case.
        from harbor_clerk.mail.exceptions import UidValidityChanged
        raise UidValidityChanged(
            f"label {label.label_path}: uidvalidity {label.uidvalidity} → {uidvalidity}"
        )

    # Search for UIDs strictly greater than last_uid_seen.
    next_uid = label.last_uid_seen + 1
    search_query = f"UID {next_uid}:*"
    search_result, search_lines = await conn.client.uid_search(search_query)
    if search_result != "OK":
        return SyncSummary(0, 0, 0)

    uids = _parse_uid_list(search_lines)
    # IMAP `UID N:*` always returns at least UIDNEXT-1 even if no messages
    # match; filter those out.
    uids = [u for u in uids if u >= next_uid]
    if not uids:
        return SyncSummary(0, 0, 0)

    uid_set = ",".join(str(u) for u in uids)
    fetch_result, fetch_lines = await conn.client.uid(
        "FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
    )
    if fetch_result != "OK":
        return SyncSummary(0, 0, 0)
    uid_to_mid = _parse_fetch_response(fetch_lines)

    new_count = 0
    duplicate_count = 0
    for uid in uids:
        message_id = uid_to_mid.get(uid) or _synthesize_message_id(uid, label.label_id)
        existing = (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            duplicate_count += 1
            continue
        placeholder_sha = hashlib.sha256(f"placeholder:{label.label_id}:{uid}".encode()).digest()
        msg = WatchedMessage(
            label_id=label.label_id,
            message_id=message_id,
            imap_uid=uid,
            eml_sha256=placeholder_sha,
            status="active",
        )
        session.add(msg)
        new_count += 1

    await session.flush()

    highest_uid = max(uids)
    await write_cursor(
        session, label.label_id, LabelCursor(last_uid_seen=highest_uid, uidvalidity=uidvalidity)
    )

    return SyncSummary(fetched_count=len(uids), new_count=new_count, duplicate_count=duplicate_count)
```

Add to `__init__.py` exports:

```python
from harbor_clerk.mail.sync import SyncSummary, sync_label_incremental, sync_label_initial
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_sync_incremental.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/sync.py src/harbor_clerk/mail/__init__.py tests/mail/test_sync_incremental.py
git commit -m "feat(mail): incremental sync — fetch UIDs > last_uid_seen"
```

---

## Task 11: UIDVALIDITY change detection → rescan

**Files:**
- Modify: `src/harbor_clerk/mail/sync.py`
- Modify: `src/harbor_clerk/mail/__init__.py`
- Create: `tests/mail/test_sync_uidvalidity.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_sync_uidvalidity.py
"""UIDVALIDITY change → drop watched_messages, restart with new epoch."""

import pytest
from sqlalchemy import select

from harbor_clerk.mail.exceptions import UidValidityChanged
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.sync import (
    handle_uidvalidity_change,
    sync_label_incremental,
)
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_incremental_raises_on_uidvalidity_mismatch(db_session, watched_label, mock_aioimap):
    """Incremental sync detects UIDVALIDITY change and refuses to advance."""
    watched_label.last_uid_seen = 10
    watched_label.uidvalidity = 12345
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 5 EXISTS",
        b"* OK [UIDVALIDITY 99999] UIDs valid",  # CHANGED
        b"OK SELECT completed",
    ])

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    with pytest.raises(UidValidityChanged):
        await sync_label_incremental(db_session, conn, watched_label)
    await conn.logout()


async def test_handle_uidvalidity_change_clears_messages_and_restarts(
    db_session, watched_label, mock_aioimap
):
    """handle_uidvalidity_change deletes old watched_messages and re-runs initial sync."""
    # Pre-populate watched_messages from old epoch
    for uid in [1, 2, 3]:
        msg = WatchedMessage(
            label_id=watched_label.label_id,
            message_id=f"<old-{uid}@example.com>",
            imap_uid=uid,
            eml_sha256=b"\x00" * 32,
            status="active",
        )
        db_session.add(msg)
    watched_label.last_uid_seen = 3
    watched_label.uidvalidity = 12345
    await db_session.flush()

    # Mock IMAP: new epoch with new UIDs
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 2 EXISTS",
        b"* OK [UIDVALIDITY 99999] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b"1 2"])
    mock_aioimap.set_uid_fetch_response("OK", [
        b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <new1@example.com>\r\n",
        b")",
        b"2 (UID 2 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <new2@example.com>\r\n",
        b")",
        b"OK FETCH completed",
    ])

    conn = IMAPConnection(host="imap.example.com", port=993, username="x", password="y")
    await conn.connect()
    await conn.login()
    summary = await handle_uidvalidity_change(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    # Old messages gone, new ones in place
    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)
        .order_by(WatchedMessage.imap_uid)
    )).scalars().all()
    assert len(rows) == 2
    assert [r.message_id for r in rows] == ["<new1@example.com>", "<new2@example.com>"]

    await db_session.refresh(watched_label)
    assert watched_label.uidvalidity == 99999
    assert watched_label.last_uid_seen == 2
    assert summary.new_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_sync_uidvalidity.py -v`
Expected: FAIL — `handle_uidvalidity_change` doesn't exist.

- [ ] **Step 3: Implement `handle_uidvalidity_change`**

Append to `src/harbor_clerk/mail/sync.py`:

```python
from sqlalchemy import delete


async def handle_uidvalidity_change(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> SyncSummary:
    """Drop all watched_messages for the label, reset cursor, run initial sync.

    Called when `sync_label_incremental` raises UidValidityChanged. Stage 3
    will react to the deleted watched_messages by soft-deleting their
    associated email Documents (via the existing 30-day reaper code path).

    Caller must have already authenticated. Caller commits.
    """
    logger.warning(
        "label %s (%s): UIDVALIDITY changed; dropping %d watched_messages and rescanning",
        label.label_id, label.label_path, label.last_uid_seen,
    )
    await session.execute(
        delete(WatchedMessage).where(WatchedMessage.label_id == label.label_id)
    )
    label.uidvalidity = None
    label.last_uid_seen = 0
    await session.flush()

    return await sync_label_initial(session, conn, label)
```

Also add to `__init__.py`:

```python
from harbor_clerk.mail.sync import (
    SyncSummary,
    handle_uidvalidity_change,
    sync_label_incremental,
    sync_label_initial,
)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_sync_uidvalidity.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/sync.py src/harbor_clerk/mail/__init__.py tests/mail/test_sync_uidvalidity.py
git commit -m "feat(mail): UIDVALIDITY change handling — drop messages, rescan"
```

---

## Task 12: IDLE supervisor + polling fallback

**Files:**
- Create: `src/harbor_clerk/mail/idle.py`
- Modify: `src/harbor_clerk/mail/__init__.py`
- Create: `tests/mail/test_idle_polling.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_idle_polling.py
"""IDLE supervisor + 2-minute polling fallback."""

import asyncio

import pytest

from harbor_clerk.mail.idle import poll_or_idle_loop, server_supports_idle
from harbor_clerk.mail.imap_client import IMAPConnection


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_server_supports_idle_true(mock_aioimap):
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1 IDLE LITERAL+"])
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    assert await server_supports_idle(conn) is True


async def test_server_supports_idle_false(mock_aioimap):
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1 LITERAL+"])  # no IDLE
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    assert await server_supports_idle(conn) is False


async def test_poll_loop_invokes_callback_on_each_tick(mock_aioimap):
    """When IDLE is unsupported, poll_or_idle_loop falls back to a 2-minute
    poll. We test with a tight interval so the test completes quickly."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1"])  # no IDLE

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()

    tick_count = 0

    async def on_tick(c: IMAPConnection) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 3:
            raise asyncio.CancelledError("done")

    with pytest.raises(asyncio.CancelledError):
        await poll_or_idle_loop(conn, on_tick=on_tick, poll_interval=0.05)

    assert tick_count == 3


async def test_idle_loop_responds_to_exists_event(mock_aioimap):
    """When server supports IDLE, the loop fires the callback on EXISTS push."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1 IDLE"])
    mock_aioimap.set_idle_events([
        b"* 5 EXISTS",
        b"* 6 EXISTS",
    ])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()

    tick_count = 0

    async def on_tick(c: IMAPConnection) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            raise asyncio.CancelledError("done")

    with pytest.raises(asyncio.CancelledError):
        await poll_or_idle_loop(conn, on_tick=on_tick, poll_interval=120)

    assert tick_count == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_idle_polling.py -v`
Expected: FAIL — `harbor_clerk.mail.idle` doesn't exist.

- [ ] **Step 3: Implement `idle.py`**

```python
# src/harbor_clerk/mail/idle.py
"""IDLE supervisor + polling fallback for the per-label sync loop.

`poll_or_idle_loop(conn, on_tick, poll_interval)` runs forever, calling
`on_tick(conn)` whenever the server signals new mail (via IDLE EXISTS) or
the poll interval expires. The caller raises CancelledError to stop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from harbor_clerk.mail.imap_client import IMAPConnection

logger = logging.getLogger(__name__)


async def server_supports_idle(conn: IMAPConnection) -> bool:
    """Issue CAPABILITY and check for IDLE."""
    result, lines = await conn.client.capability()
    if result != "OK":
        return False
    blob = b" ".join(lines).upper()
    return b"IDLE" in blob.split()


async def poll_or_idle_loop(
    conn: IMAPConnection,
    on_tick: Callable[[IMAPConnection], Awaitable[None]],
    poll_interval: float = 120.0,
    idle_timeout: float = 29 * 60,  # Gmail recommends < 30 minutes
) -> None:
    """Run forever, calling on_tick on each IDLE EXISTS or poll timeout.

    Strategy:
      - Probe CAPABILITY for IDLE.
      - If supported: open IDLE, race wait_server_push against asyncio
        timeout=idle_timeout. On either, exit IDLE, call on_tick, restart.
      - If not supported: sleep poll_interval, call on_tick, loop.

    The callback exiting via CancelledError ends the loop.
    """
    if await server_supports_idle(conn):
        logger.info("conn %s: using IDLE (timeout=%.0fs)", conn.host, idle_timeout)
        while True:
            try:
                await conn.client.idle_start(timeout=idle_timeout)
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(
                                conn.client.wait_server_push(), timeout=idle_timeout
                            )
                        except asyncio.TimeoutError:
                            break  # IDLE refresh; no events
                        if b"EXISTS" in event or b"EXPUNGE" in event:
                            break
                finally:
                    await conn.client.idle_done()
                await on_tick(conn)
            except asyncio.CancelledError:
                # Try to clean up IDLE state best-effort
                try:
                    await conn.client.idle_done()
                except Exception:
                    pass
                raise
    else:
        logger.info("conn %s: IDLE unsupported, polling every %.0fs", conn.host, poll_interval)
        while True:
            try:
                await on_tick(conn)
                await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
```

Add to `__init__.py`:

```python
from harbor_clerk.mail.idle import poll_or_idle_loop, server_supports_idle
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_idle_polling.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/idle.py src/harbor_clerk/mail/__init__.py tests/mail/test_idle_polling.py
git commit -m "feat(mail): IDLE supervisor + polling fallback"
```

---

## Task 13: Lifecycle — detect missing UIDs → status='unlabeled'

**Files:**
- Create: `src/harbor_clerk/mail/lifecycle.py`
- Modify: `src/harbor_clerk/mail/__init__.py`
- Create: `tests/mail/test_lifecycle.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_lifecycle.py
"""Lifecycle: detect messages that left the label, mark as 'unlabeled'."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.lifecycle import detect_unlabeled_messages
from harbor_clerk.models import WatchedMessage


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_detect_unlabeled_marks_missing_messages(db_session, watched_label, mock_aioimap):
    """Three messages were active; server now reports only two — mark
    the missing one as 'unlabeled'."""
    for uid in [1, 2, 3]:
        msg = WatchedMessage(
            label_id=watched_label.label_id,
            message_id=f"<msg{uid}@example.com>",
            imap_uid=uid,
            eml_sha256=b"\x00" * 32,
            status="active",
        )
        db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 2 EXISTS",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b"1 3"])  # UID 2 is gone

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    unlabeled_count = await detect_unlabeled_messages(db_session, conn, watched_label)
    await conn.logout()
    await db_session.commit()

    assert unlabeled_count == 1

    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == watched_label.label_id)
        .order_by(WatchedMessage.imap_uid)
    )).scalars().all()
    statuses = {r.imap_uid: r.status for r in rows}
    unlabeled_at = {r.imap_uid: r.unlabeled_at for r in rows}
    assert statuses == {1: "active", 2: "unlabeled", 3: "active"}
    assert unlabeled_at[2] is not None
    assert unlabeled_at[1] is None
    assert unlabeled_at[3] is None


async def test_detect_unlabeled_idempotent(db_session, watched_label, mock_aioimap):
    """Running twice doesn't re-trigger unlabeled_at on already-unlabeled rows."""
    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<gone@example.com>",
        imap_uid=1,
        eml_sha256=b"\x00" * 32,
        status="unlabeled",
        unlabeled_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(msg)
    await db_session.flush()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_select_response("OK", [
        b"* 0 EXISTS",
        b"* OK [UIDVALIDITY 12345] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b""])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    unlabeled_count = await detect_unlabeled_messages(db_session, conn, watched_label)
    await conn.logout()

    assert unlabeled_count == 0  # nothing newly unlabeled
    refetched = (await db_session.execute(select(WatchedMessage))).scalars().first()
    assert refetched.unlabeled_at == datetime(2026, 1, 1, tzinfo=UTC)  # unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_lifecycle.py -v`
Expected: FAIL — `harbor_clerk.mail.lifecycle` doesn't exist.

- [ ] **Step 3: Implement `lifecycle.py`**

```python
# src/harbor_clerk/mail/lifecycle.py
"""Lifecycle handling for watched_messages.

When a message is removed from the watched label (un-labeled in Gmail's
UI, or permanently deleted in any provider), our cursor-based incremental
sync doesn't notice — we never see the message disappear because we only
look forward from `last_uid_seen`. This module does the periodic check:
SEARCH ALL UIDs in the label, diff against active watched_messages, mark
any missing ones as 'unlabeled'.

Stage 3 will read these unlabeled rows and soft-delete the associated
email Documents (and their attachment Documents) via the existing 30-day
reaper code path.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.models import WatchedLabel, WatchedMessage

logger = logging.getLogger(__name__)


async def detect_unlabeled_messages(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> int:
    """Diff active watched_messages against the server-side UID set. Mark
    any UIDs in our DB but not on the server as 'unlabeled'.

    Returns the count of rows newly transitioned to 'unlabeled'.

    Caller must have already authenticated. Caller commits.
    """
    # Get current server-side UID set
    select_result, _select_lines = await conn.client.select(label.label_path)
    if select_result != "OK":
        logger.warning("SELECT %r failed in lifecycle scan", label.label_path)
        return 0
    search_result, search_lines = await conn.client.uid_search("ALL")
    if search_result != "OK":
        return 0

    server_uids: set[int] = set()
    for line in search_lines:
        for tok in line.split():
            try:
                server_uids.add(int(tok))
            except ValueError:
                continue

    # Get all currently-active watched_messages for this label
    active_rows = (
        await session.execute(
            select(WatchedMessage).where(
                WatchedMessage.label_id == label.label_id,
                WatchedMessage.status == "active",
            )
        )
    ).scalars().all()

    # Find ones missing from server
    now = datetime.now(UTC)
    transitioned = 0
    for row in active_rows:
        if row.imap_uid not in server_uids:
            row.status = "unlabeled"
            row.unlabeled_at = now
            transitioned += 1

    if transitioned:
        await session.flush()
        logger.info(
            "label %s (%s): transitioned %d watched_messages to 'unlabeled'",
            label.label_id, label.label_path, transitioned,
        )
    return transitioned
```

Add to `__init__.py`:

```python
from harbor_clerk.mail.lifecycle import detect_unlabeled_messages
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_lifecycle.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/lifecycle.py src/harbor_clerk/mail/__init__.py tests/mail/test_lifecycle.py
git commit -m "feat(mail): lifecycle — detect missing UIDs, mark as unlabeled"
```

---

## Task 14: Mail observer — per-label sync supervisor

**Files:**
- Create: `src/harbor_clerk/watcher/mail_observer.py`
- Create: `tests/mail/test_mail_observer.py`

The mail observer is the asyncio orchestrator: it queries `watched_labels` for active rows, opens one IMAP connection per label, runs the per-label loop, and supervises restarts. It's NOT yet wired into the watcher daemon — that happens in Task 15.

- [ ] **Step 1: Write the failing test**

```python
# tests/mail/test_mail_observer.py
"""Mail observer: launches per-label sync tasks, supervises restarts."""

import asyncio
import base64
import os

import pytest
from sqlalchemy import select

from harbor_clerk.models import MailAccount, WatchedLabel, WatchedMessage
from harbor_clerk.secrets import get_cipher
from harbor_clerk.watcher.mail_observer import MailObserver


@pytest.fixture(autouse=True)
def setup_master_key(monkeypatch):
    monkeypatch.setenv(
        "HARBOR_CLERK_MASTER_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    from harbor_clerk.secrets import _accessor
    _accessor.reset()
    yield
    _accessor.reset()


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP
    FakeIMAP.reset()
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_observer_runs_initial_sync_for_active_label(
    db_session, mock_aioimap, monkeypatch
):
    """Observer started against a label with no cursor → runs initial sync."""
    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="obs-test", provider="gmail",
        imap_host="imap.gmail.com", imap_port=993,
        imap_username="obs@example.com",
        app_password_ciphertext=ct, key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id, label_path="Obs", display_name="Obs",
    )
    db_session.add(label)
    await db_session.commit()

    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1"])  # no IDLE → polling
    mock_aioimap.set_select_response("OK", [
        b"* 1 EXISTS",
        b"* OK [UIDVALIDITY 5555] UIDs valid",
        b"OK SELECT completed",
    ])
    mock_aioimap.set_uid_search_response("OK", [b"1"])
    mock_aioimap.set_uid_fetch_response("OK", [
        b"1 (UID 1 BODY[HEADER.FIELDS (MESSAGE-ID)] {32}",
        b"Message-ID: <obs1@example.com>\r\n",
        b")",
        b"OK FETCH completed",
    ])

    observer = MailObserver(poll_interval=0.05)

    # Run for ~0.3s — long enough for one initial-sync tick to complete
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.3)
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == label.label_id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].message_id == "<obs1@example.com>"


async def test_observer_skips_paused_labels(db_session, mock_aioimap):
    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="paused-test", provider="gmail",
        imap_host="imap.gmail.com", imap_port=993,
        imap_username="paused@example.com",
        app_password_ciphertext=ct, key_fingerprint=fp,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id, label_path="Paused",
        display_name="Paused", status="paused",
    )
    db_session.add(label)
    await db_session.commit()

    mock_aioimap.set_login_response("OK", b"OK")  # never called

    observer = MailObserver(poll_interval=0.05)
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.2)
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    # No watched_messages should have been created
    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == label.label_id)
    )).scalars().all()
    assert rows == []


async def test_observer_skips_auth_error_accounts(db_session, mock_aioimap):
    cipher = get_cipher()
    ct, fp = cipher.encrypt(b"app-pw")
    account = MailAccount(
        display_name="auth-err-test", provider="gmail",
        imap_host="imap.gmail.com", imap_port=993,
        imap_username="autherr@example.com",
        app_password_ciphertext=ct, key_fingerprint=fp,
        status="auth_error",
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id, label_path="WontSync",
        display_name="WontSync",
    )
    db_session.add(label)
    await db_session.commit()

    observer = MailObserver(poll_interval=0.05)
    task = asyncio.create_task(observer.run())
    await asyncio.sleep(0.2)
    await observer.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass

    rows = (await db_session.execute(
        select(WatchedMessage).where(WatchedMessage.label_id == label.label_id)
    )).scalars().all()
    assert rows == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/mail/test_mail_observer.py -v`
Expected: FAIL — `harbor_clerk.watcher.mail_observer` doesn't exist.

- [ ] **Step 3: Implement `mail_observer.py`**

```python
# src/harbor_clerk/watcher/mail_observer.py
"""Mail observer: orchestrates per-label sync tasks under the watcher daemon.

Architecture:
  - One MailObserver per process.
  - On each supervisor tick (every `supervisor_interval` seconds):
      - Query DB for `watched_labels` where status='active' AND
        account.status='active'.
      - For each label not yet watched: spawn a sync task.
      - For each watched label that's no longer in the active set: cancel
        its task.
  - Per-label sync task: open IMAP connection, run initial-or-incremental
    sync, enter poll_or_idle_loop with on_tick = (incremental sync +
    lifecycle scan). On AuthError: mark account, exit. On UidValidityChanged:
    rescan, continue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from harbor_clerk.config import get_settings
from harbor_clerk.db import get_async_engine
from harbor_clerk.mail.exceptions import AuthError, UidValidityChanged
from harbor_clerk.mail.idle import poll_or_idle_loop
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.lifecycle import detect_unlabeled_messages
from harbor_clerk.mail.sync import (
    handle_uidvalidity_change,
    sync_label_incremental,
    sync_label_initial,
)
from harbor_clerk.models import MailAccount, WatchedLabel
from harbor_clerk.secrets import get_cipher

logger = logging.getLogger(__name__)


class MailObserver:
    def __init__(
        self,
        *,
        supervisor_interval: float = 5.0,
        poll_interval: float = 120.0,
    ):
        self.supervisor_interval = supervisor_interval
        self.poll_interval = poll_interval
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()

    async def stop(self) -> None:
        """Signal the supervisor and all per-label tasks to stop."""
        self._stop_event.set()
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def run(self) -> None:
        """Supervisor loop. Returns when stop() is called."""
        engine = get_async_engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            while not self._stop_event.is_set():
                await self._reconcile(session_factory)
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.supervisor_interval
                    )
                    break  # stop_event was set
                except asyncio.TimeoutError:
                    continue  # tick again
        finally:
            await self.stop()

    async def _reconcile(self, session_factory) -> None:
        """Spawn / cancel per-label tasks to match the desired-state DB query."""
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(WatchedLabel)
                    .options(selectinload(WatchedLabel.account))
                    .where(WatchedLabel.status == "active")
                )
            ).scalars().all()

        desired_ids = {
            lbl.label_id for lbl in rows if lbl.account.status == "active"
        }
        actual_ids = set(self._tasks.keys())

        # Spawn tasks for newly-active labels
        for lbl in rows:
            if lbl.account.status != "active":
                continue
            if lbl.label_id not in actual_ids:
                self._tasks[lbl.label_id] = asyncio.create_task(
                    self._run_label(lbl.label_id, session_factory)
                )

        # Cancel tasks for labels that are no longer active
        for lid in actual_ids - desired_ids:
            task = self._tasks.pop(lid, None)
            if task is not None:
                task.cancel()

    async def _run_label(self, label_id: UUID, session_factory) -> None:
        """Per-label sync task. Opens an IMAP connection, runs sync until cancelled or auth-fails."""
        async with session_factory() as session:
            label = (
                await session.execute(
                    select(WatchedLabel)
                    .options(selectinload(WatchedLabel.account))
                    .where(WatchedLabel.label_id == label_id)
                )
            ).scalar_one()
            account = label.account

            cipher = get_cipher()
            try:
                password = cipher.decrypt(
                    account.app_password_ciphertext, account.key_fingerprint
                ).decode()
            except Exception as exc:
                logger.warning("decrypt failed for account %s: %s", account.account_id, exc)
                account.status = "key_mismatch"
                account.last_error = "key fingerprint does not match active master key"
                await session.commit()
                return

            conn = IMAPConnection(
                host=account.imap_host,
                port=account.imap_port,
                username=account.imap_username,
                password=password,
            )

        try:
            await conn.connect()
            await conn.login()
        except AuthError as exc:
            async with session_factory() as session:
                acc = (
                    await session.execute(select(MailAccount).where(MailAccount.account_id == account.account_id))
                ).scalar_one()
                acc.status = "auth_error"
                acc.last_error = str(exc)
                await session.commit()
            return
        except Exception as exc:
            logger.warning("connect/login failed for label %s: %s", label_id, exc)
            return

        async def on_tick(c: IMAPConnection) -> None:
            async with session_factory() as session:
                lbl = (
                    await session.execute(
                        select(WatchedLabel).where(WatchedLabel.label_id == label_id)
                    )
                ).scalar_one()
                try:
                    if lbl.uidvalidity is None:
                        await sync_label_initial(session, c, lbl)
                    else:
                        try:
                            await sync_label_incremental(session, c, lbl)
                        except UidValidityChanged:
                            await handle_uidvalidity_change(session, c, lbl)
                    await detect_unlabeled_messages(session, c, lbl)
                    lbl.last_synced_at = datetime.now(UTC)
                    await session.commit()
                except Exception as exc:
                    logger.exception("sync failed for label %s: %s", label_id, exc)
                    await session.rollback()

        try:
            # Initial tick (in case label has no cursor yet)
            await on_tick(conn)
            await poll_or_idle_loop(conn, on_tick=on_tick, poll_interval=self.poll_interval)
        except asyncio.CancelledError:
            pass
        finally:
            await conn.logout()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/mail/test_mail_observer.py -v`
Expected: PASS — all 3 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/mail_observer.py tests/mail/test_mail_observer.py
git commit -m "feat(watcher): MailObserver — per-label sync supervisor"
```

---

## Task 15: Wire mail observer into watcher daemon

**Files:**
- Modify: `src/harbor_clerk/watcher/main.py`

The existing `watcher/main.py` currently launches the filesystem `Observer`. Task 15 makes it also launch the `MailObserver` alongside.

- [ ] **Step 1: Read the current watcher main**

```bash
cat src/harbor_clerk/watcher/main.py
```

Identify the entry point function (usually `main()` or `async def run()`). Note the lifecycle: signal handling, observer start, observer stop.

- [ ] **Step 2: Modify the watcher main to also launch MailObserver**

In `watcher/main.py`, find the place where the filesystem `Observer` is started in the asyncio event loop. Add the mail observer alongside:

```python
# After the imports, alongside the existing Observer import:
from harbor_clerk.watcher.mail_observer import MailObserver

# In the main async run function (whatever it's called), after starting
# the filesystem observer, add:
mail_observer = MailObserver()
mail_task = asyncio.create_task(mail_observer.run())

# In the shutdown handler (or finally block), before exiting:
await mail_observer.stop()
try:
    await asyncio.wait_for(mail_task, timeout=10.0)
except asyncio.TimeoutError:
    logger.warning("mail observer did not stop within 10s; forcing cancel")
    mail_task.cancel()
```

The exact placement depends on the existing structure; the key invariants are:
1. `mail_observer.run()` is launched as a Task (concurrent with filesystem observer).
2. On shutdown, `mail_observer.stop()` is awaited before the process exits.
3. `mail_task` is awaited (with timeout) so logs flush cleanly.

- [ ] **Step 3: Verify no regressions in the watcher startup**

There's no easy unit test for "watcher launches MailObserver" without spinning up a real watcher process. Instead, verify by importing — the watcher main should still import cleanly:

```bash
uv run python -c "from harbor_clerk.watcher.main import main; print('imports OK')"
```

Expected: `imports OK`.

- [ ] **Step 4: Run all watcher tests to catch regressions**

```bash
uv run pytest tests/watcher/ -v
```

Expected: PASS — no regressions in existing watcher tests.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/watcher/main.py
git commit -m "feat(watcher): launch MailObserver alongside filesystem Observer"
```

---

## Task 16: Manual rescan endpoint

**Files:**
- Modify: `src/harbor_clerk/api/routes/mail.py`
- Modify: `tests/api/test_mail_routes.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/api/test_mail_routes.py`:

```python
async def test_rescan_label_resets_cursor(client, admin_user, admin_token, db_session):
    from harbor_clerk.models import WatchedLabel
    body = {
        "display_name": "rescan", "provider": "gmail", "imap_host": "imap.gmail.com",
        "imap_port": 993, "imap_username": "rescan@example.com", "app_password": "x",
    }
    create = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create.json()["account_id"]
    label_resp = await client.post(
        "/api/mail/labels",
        json={"account_id": account_id, "label_path": "Rescan", "display_name": "Rescan"},
        headers=auth_header(admin_token),
    )
    label_id = label_resp.json()["label_id"]

    # Manually set a cursor
    label = (await db_session.execute(
        select(WatchedLabel).where(WatchedLabel.label_id == label_id)
    )).scalar_one()
    label.last_uid_seen = 100
    label.uidvalidity = 12345
    await db_session.commit()

    # Trigger rescan
    resp = await client.post(
        f"/api/mail/labels/{label_id}/rescan",
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "rescan-queued"

    # Cursor should be reset
    await db_session.refresh(label)
    assert label.last_uid_seen == 0
    assert label.uidvalidity is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/api/test_mail_routes.py::test_rescan_label_resets_cursor -v`
Expected: FAIL — 404.

- [ ] **Step 3: Implement the rescan endpoint**

Append to `src/harbor_clerk/api/routes/mail.py`:

```python
@router.post(
    "/labels/{label_id}/rescan",
    status_code=status.HTTP_202_ACCEPTED,
)
async def rescan_label(
    label_id: UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Trigger a full rescan of a watched label.

    Resets the cursor (uidvalidity, last_uid_seen) so the supervisor's next
    tick treats this label as new and runs an initial sync. Existing
    watched_messages rows are preserved — re-sync hits the dedup check and
    no-ops on rows whose Message-IDs are already known.

    Returns 202 Accepted; the actual rescan happens asynchronously.
    """
    label = (
        await session.execute(select(WatchedLabel).where(WatchedLabel.label_id == label_id))
    ).scalar_one_or_none()
    if label is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="watched label not found")
    label.uidvalidity = None
    label.last_uid_seen = 0
    await session.commit()
    return {"status": "rescan-queued"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/api/test_mail_routes.py::test_rescan_label_resets_cursor -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/mail.py tests/api/test_mail_routes.py
git commit -m "feat(api): POST /api/mail/labels/{id}/rescan — reset cursor"
```

---

## Task 17: Dovecot end-to-end integration test (optional — run only when dovecot is available)

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_mail_e2e_dovecot.py`
- Modify: `pyproject.toml` — add `integration` pytest marker

The unit tests so far use the in-process `FakeIMAP`. This task adds one end-to-end test against a real Dovecot server (running in Docker) that exercises the full chain. It's marked `@pytest.mark.integration` and skipped by default; CI runs it on a separate path with the Dovecot container up.

- [ ] **Step 1: Add the `integration` pytest marker**

In `pyproject.toml`'s `[tool.pytest.ini_options]` section, add:

```toml
markers = [
    "integration: end-to-end tests requiring external services (Dovecot, etc.). Run with `uv run pytest -m integration`.",
]
```

- [ ] **Step 2: Write the integration test**

```python
# tests/integration/__init__.py — empty
```

```python
# tests/integration/test_mail_e2e_dovecot.py
"""End-to-end test against a real Dovecot IMAP server.

Skipped by default. To run: ensure Dovecot is reachable on
DOVECOT_TEST_HOST:DOVECOT_TEST_PORT (default localhost:1143) with a known
test account, then `uv run pytest -m integration tests/integration/`.

The dovecot setup is documented in docker/dovecot.test.Dockerfile (a
follow-up task can ship a docker-compose.test.yml that brings it up).

This test does NOT use `FakeIMAP` — it goes through real aioimaplib to
the real server.
"""

from __future__ import annotations

import os
import socket

import pytest

from harbor_clerk.mail import IMAPConnection, discover_folders
from harbor_clerk.mail.sync import sync_label_initial

pytestmark = pytest.mark.integration

DOVECOT_HOST = os.environ.get("DOVECOT_TEST_HOST", "localhost")
DOVECOT_PORT = int(os.environ.get("DOVECOT_TEST_PORT", "1143"))
DOVECOT_USER = os.environ.get("DOVECOT_TEST_USER", "testuser")
DOVECOT_PASSWORD = os.environ.get("DOVECOT_TEST_PASSWORD", "testpass")


def _dovecot_reachable() -> bool:
    try:
        with socket.create_connection((DOVECOT_HOST, DOVECOT_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark_skip = pytest.mark.skipif(
    not _dovecot_reachable(),
    reason=f"Dovecot not reachable at {DOVECOT_HOST}:{DOVECOT_PORT}",
)


@pytestmark_skip
async def test_e2e_connect_login_list_folders():
    conn = IMAPConnection(
        host=DOVECOT_HOST, port=DOVECOT_PORT,
        username=DOVECOT_USER, password=DOVECOT_PASSWORD,
    )
    await conn.connect()
    await conn.login()
    folders = await discover_folders(conn)
    await conn.logout()
    paths = [f.path for f in folders]
    assert "INBOX" in paths


# Note: a full sync_label_initial e2e test requires injecting test mail
# into the dovecot container first. That setup belongs in a separate
# integration suite and is deferred — the unit tests cover the sync
# logic; this file just verifies aioimaplib + our wrapper actually talk
# to a real server.
```

- [ ] **Step 3: Run the integration test (will skip if Dovecot not reachable)**

```bash
uv run pytest -m integration tests/integration/test_mail_e2e_dovecot.py -v
```

Expected: SKIPPED (Dovecot not configured) OR PASSED (if you've set up Dovecot).

Verify the marker filters work: regular `uv run pytest` should NOT run integration tests (they should be excluded by default unless explicitly selected with `-m integration`).

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml tests/integration/
git commit -m "test(mail): integration smoke test against real Dovecot (skipped by default)"
```

---

## Wrap-up

After Task 17 commits, Stage 2 is complete. Run the full verification suite:

- [ ] **Run the full Python test suite**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage2 && uv run pytest -v 2>&1 | tail -10
```

Expected: every Python test green; integration tests skipped by default.

- [ ] **Run linting**

```bash
uv run ruff check . && uv run ruff format --check .
```

Expected: clean.

- [ ] **Run security scan**

```bash
uv run pip-audit --desc 2>&1 | tail -5
```

Expected: no HIGH-severity findings.

- [ ] **Manual smoke test**

(If Stage 1 is merged or rebased onto this branch — i.e., schema migration applied to your dev DB.)

```bash
# Set the master key
export HARBOR_CLERK_MASTER_KEY=$(python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')

# Start the API
uv run harbor-clerk-api &

# Create an account (against a real Gmail account with an app password)
curl -X POST http://localhost:8000/api/mail/accounts \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"display_name":"Personal","provider":"gmail","imap_host":"imap.gmail.com","imap_port":993,"imap_username":"you@gmail.com","app_password":"your-app-password"}'

# Test the connection
curl -X POST http://localhost:8000/api/mail/accounts/<account_id>/test \
    -H "Authorization: Bearer $ADMIN_TOKEN"

# Pick a label and watch it
curl -X POST http://localhost:8000/api/mail/labels \
    -H "Authorization: Bearer $ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"account_id":"<account_id>","label_path":"Clerk","display_name":"Clerk"}'

# Start the watcher
uv run harbor-clerk-watcher &

# Drop a few emails into the Gmail "Clerk" label, then watch:
psql -d lka -c "SELECT label_id, message_id, imap_uid, status FROM watched_messages;"
```

Expected: rows appear in `watched_messages` as you label emails in Gmail.

- [ ] **Open Stage 2 PR**

```bash
git push -u origin spec/email-stage2
gh pr create --title "feat(email): stage 2 IMAP sync engine — connect, watch, populate watched_messages" \
    --body-file docs/superpowers/plans/stage2-pr-body.md  # template the body separately
```

The PR description should:
- Link to the spec and this plan
- Note that Stage 2 is **headless** (no Document creation, no UI)
- Note that it depends on Stage 1 (PR #281)
- Document the e2e Dovecot test as opt-in
- Stage roadmap (3 → email→Document pipeline; 4 → UI)
