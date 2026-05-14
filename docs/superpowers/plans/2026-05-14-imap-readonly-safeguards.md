# IMAP Read-Only Safeguards + Audit Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Harbor Clerk's existing IMAP mail ingestion provably read-only at three independent layers (protocol, client wrapper, library subclass), and add a redaction-aware audit log of every IMAP command issued.

**Architecture:** The existing `IMAPConnection` (`src/harbor_clerk/mail/imap_client.py`) is a thin wrapper around `aioimaplib.IMAP4_SSL` that currently leaks the underlying client via a `.client` property — 11 call sites bypass the wrapper, and 3 of them issue `select()` (which opens the mailbox read-write). We turn the wrapper into a closed seam (no escape hatch), add explicit allow-listed methods (`examine`, `fetch`, `uid`, `uid_search`, `list`, `capability`, `idle_*`), gate `uid()` against mutating subcommands, and back the wrapper with a `ReadOnlyIMAP4_SSL(aioimaplib.IMAP4_SSL)` subclass that raises on every mutating method even if someone bypasses the wrapper. The wrapper records each command to a new `imap_command_log` table with `LOGIN`/`XOAUTH2` args redacted; a periodic reaper drops rows older than 30 days. A no-mutation invariant is enforced in unit tests via `FakeIMAP` and in integration tests against real Dovecot via a pre/post snapshot diff.

**Tech Stack:** Python 3.12, `aioimaplib`, SQLAlchemy 2.0 async, Alembic, PostgreSQL 18, pytest, existing Dovecot integration-test harness (`tests/integration/test_mail_e2e_dovecot.py`).

**Layers covered (from the brainstorm):**
- **Layer 1 (EXAMINE not SELECT):** Tasks 1–3.
- **Layer 2 (wrapped client + subclass):** Tasks 1, 4–8.
- **Layer 3 (OAuth read-only scopes):** Out of scope — the codebase has no OAuth yet (only password auth via `mail_accounts.app_password_ciphertext`). When OAuth is added, scopes must be `gmail.readonly` / `Mail.Read`. Recorded in `pr_followups.md` as a constraint.
- **Layer 4 (per-provider docs):** Task 16.
- **Layer 5 (CI integration test against real IMAP):** Task 14.
- **Audit log:** Tasks 9–13.

---

## File Structure

**Create:**
- `src/harbor_clerk/mail/readonly_imap.py` — `ReadOnlyIMAP4_SSL` subclass + `ReadOnlyViolation` exception.
- `src/harbor_clerk/mail/audit.py` — `log_imap_command()` helper + `redact_imap_args()` + `reap_old_imap_command_logs()`.
- `src/harbor_clerk/models/imap_command_log.py` — SQLAlchemy model.
- `alembic/versions/0022_imap_command_log.py` — Migration.
- `tests/mail/test_readonly_imap.py` — Subclass mutation-block tests.
- `tests/mail/test_imap_audit.py` — Logger + redaction + reaper tests.
- `tests/mail/test_no_mutation_static.py` — Static assertion that no mail module calls a mutating IMAP method.
- `docs/imap-readonly.md` — Per-provider safeguards / setup notes.

**Modify:**
- `src/harbor_clerk/mail/imap_client.py` — Close the seam; add allow-listed methods; remove `.client`; wire `ReadOnlyIMAP4_SSL`; wire audit logger.
- `src/harbor_clerk/mail/exceptions.py` — Add `ReadOnlyViolation`.
- `src/harbor_clerk/mail/sync.py:105, 185` — Replace `select` with wrapper `examine()`.
- `src/harbor_clerk/mail/sync.py:113, 123, 200, 212` — Replace `conn.client.uid_search` / `conn.client.uid` with wrapper methods.
- `src/harbor_clerk/mail/lifecycle.py:42, 46` — Replace `select` + `uid_search`.
- `src/harbor_clerk/mail/labels.py:87` — Replace `conn.client.list`.
- `src/harbor_clerk/mail/idle.py:21, 48, 52, 67` — Replace `capability`, `idle_start`, `wait_server_push`, `idle_done`.
- `src/harbor_clerk/mail/ingest.py:58` — Replace `conn.client.uid` FETCH.
- `tests/mail/conftest.py` — Tighten `FakeIMAP`: remove `select()`, add `examine()`, raise on mutating methods.
- `src/harbor_clerk/models/__init__.py` — Export `ImapCommandLog`.
- `src/harbor_clerk/app.py` — Schedule `reap_old_imap_command_logs()` in the existing periodic-tasks loop.
- `tests/integration/test_mail_e2e_dovecot.py` — Add `test_no_mutation_invariant`.

**Migration:**
- `0022_imap_command_log` (down rev `0021`).

---

## Task 1: Add `ReadOnlyViolation` exception and `examine()` to `IMAPConnection`

**Files:**
- Modify: `src/harbor_clerk/mail/exceptions.py`
- Modify: `src/harbor_clerk/mail/imap_client.py`
- Test: `tests/mail/test_imap_client.py`

- [ ] **Step 1: Read the existing exceptions module to match style**

Run: `cat src/harbor_clerk/mail/exceptions.py`

Skim the module to see how `AuthError` is defined; the new exception should follow the same shape.

- [ ] **Step 2: Add `ReadOnlyViolation` to `src/harbor_clerk/mail/exceptions.py`**

Append:

```python
class ReadOnlyViolation(RuntimeError):
    """Raised when code attempts an IMAP command that could mutate
    server state. Harbor Clerk's IMAP access is strictly read-only —
    no flag changes, no folder mutations, no APPEND, no STORE."""
```

- [ ] **Step 3: Write the failing test for `IMAPConnection.examine()`**

Add to `tests/mail/test_imap_client.py`:

```python
async def test_examine_uses_examine_not_select(_patch_aioimaplib, monkeypatch):
    """examine() must call the underlying client's examine(), never select().

    Rationale: select() opens the mailbox read-write; examine() opens it
    read-only and the IMAP server itself rejects mutations on the selection.
    """
    from harbor_clerk.mail.imap_client import IMAPConnection
    from tests.mail.conftest import FakeIMAP

    calls: list[str] = []

    async def _fake_examine(mailbox):
        calls.append(f"examine:{mailbox}")
        return "OK", [b"OK [READ-ONLY]"]

    async def _fake_select(mailbox):  # should never be called
        calls.append(f"select:{mailbox}")
        return "OK", [b"OK [READ-WRITE]"]

    monkeypatch.setattr(FakeIMAP, "examine", _fake_examine, raising=False)
    monkeypatch.setattr(FakeIMAP, "select", _fake_select, raising=False)

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    result, _lines = await conn.examine("INBOX")

    assert result == "OK"
    assert calls == ["examine:INBOX"]
```

- [ ] **Step 4: Run the test and verify it fails**

Run: `uv run pytest tests/mail/test_imap_client.py::test_examine_uses_examine_not_select -v`

Expected: FAIL with `AttributeError: 'IMAPConnection' object has no attribute 'examine'`.

- [ ] **Step 5: Add `examine()` to `IMAPConnection` in `src/harbor_clerk/mail/imap_client.py`**

Insert after the existing `logout()` method (before the `client` property):

```python
    async def examine(self, mailbox: str) -> tuple[str, list[bytes]]:
        """Open `mailbox` in read-only mode (IMAP EXAMINE command).

        Server-enforced: STORE/EXPUNGE/COPY/MOVE/APPEND on the selection
        will be rejected by the server. Callers should always prefer this
        over `select()` — `select()` is intentionally not exposed.
        """
        if self._client is None or not self._logged_in:
            raise RuntimeError("examine() called before login()")
        return await self._client.examine(mailbox)
```

- [ ] **Step 6: Run the test and verify it passes**

Run: `uv run pytest tests/mail/test_imap_client.py::test_examine_uses_examine_not_select -v`

Expected: PASS.

- [ ] **Step 7: Run the full mail-client test file to confirm no regression**

Run: `uv run pytest tests/mail/test_imap_client.py -v`

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_clerk/mail/exceptions.py src/harbor_clerk/mail/imap_client.py tests/mail/test_imap_client.py
git commit -m "feat(mail): add IMAPConnection.examine() + ReadOnlyViolation

EXAMINE opens the mailbox read-only at the protocol level. Callers
will be migrated off the bare select() escape hatch in follow-ups."
```

---

## Task 2: Migrate the three `select()` call sites to `examine()`

**Files:**
- Modify: `src/harbor_clerk/mail/sync.py:105`
- Modify: `src/harbor_clerk/mail/sync.py:185`
- Modify: `src/harbor_clerk/mail/lifecycle.py:42`
- Test: `tests/mail/test_sync_initial.py`, `tests/mail/test_sync_incremental.py`, `tests/mail/test_lifecycle.py`

- [ ] **Step 1: Read each call site to capture the exact diff context**

Run: `grep -n "conn.client.select" src/harbor_clerk/mail/sync.py src/harbor_clerk/mail/lifecycle.py`

Expected output:
```
src/harbor_clerk/mail/sync.py:105:    select_result, select_lines = await conn.client.select(label.label_path)
src/harbor_clerk/mail/sync.py:185:    select_result, select_lines = await conn.client.select(label.label_path)
src/harbor_clerk/mail/lifecycle.py:42:    select_result, _select_lines = await conn.client.select(label.label_path)
```

- [ ] **Step 2: Write the failing test — initial sync must call examine**

Add to `tests/mail/test_sync_initial.py`:

```python
async def test_initial_sync_uses_examine_not_select(_patch_aioimaplib, mail_account, watched_label, db_session, monkeypatch):
    """The initial sync must open the mailbox read-only."""
    from harbor_clerk.mail import sync
    from harbor_clerk.mail.imap_client import IMAPConnection
    from tests.mail.conftest import FakeIMAP

    seen: list[str] = []

    async def _examine(self, mailbox):
        seen.append(("examine", mailbox))
        return "OK", [b"* OK [UIDVALIDITY 12345]"]

    async def _select(self, mailbox):
        seen.append(("select", mailbox))
        return "OK", [b"* OK [UIDVALIDITY 12345]"]

    monkeypatch.setattr(FakeIMAP, "examine", _examine, raising=False)
    monkeypatch.setattr(FakeIMAP, "select", _select, raising=False)
    FakeIMAP.set_uid_search_response("OK", [b"* SEARCH"])

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    await sync.initial_sync(db_session, conn, watched_label)

    assert any(call[0] == "examine" for call in seen), f"examine was never called: {seen}"
    assert not any(call[0] == "select" for call in seen), f"select must never be called: {seen}"
```

- [ ] **Step 3: Run the test and verify it fails**

Run: `uv run pytest tests/mail/test_sync_initial.py::test_initial_sync_uses_examine_not_select -v`

Expected: FAIL — `select` is in `seen`.

- [ ] **Step 4: Replace `select` with `examine` in `src/harbor_clerk/mail/sync.py:105`**

```python
    select_result, select_lines = await conn.examine(label.label_path)
```

(Variable names retained for diff minimality; we'll rename in a later cleanup task only if needed.)

- [ ] **Step 5: Replace `select` with `examine` in `src/harbor_clerk/mail/sync.py:185`**

Same change as Step 4.

- [ ] **Step 6: Replace `select` with `examine` in `src/harbor_clerk/mail/lifecycle.py:42`**

```python
    select_result, _select_lines = await conn.examine(label.label_path)
```

- [ ] **Step 7: Run the initial-sync test and verify it passes**

Run: `uv run pytest tests/mail/test_sync_initial.py::test_initial_sync_uses_examine_not_select -v`

Expected: PASS.

- [ ] **Step 8: Run all mail tests to confirm no regression**

Run: `uv run pytest tests/mail/ -v`

Expected: all PASS. If sync.py's variable name `select_result` looks awkward now, leave it — pure rename, no behavior change, defer to a later sweep.

- [ ] **Step 9: Commit**

```bash
git add src/harbor_clerk/mail/sync.py src/harbor_clerk/mail/lifecycle.py tests/mail/test_sync_initial.py
git commit -m "fix(mail): use EXAMINE instead of SELECT for all mailbox opens

select() opens the mailbox read-write; the IMAP server happily accepts
STORE/EXPUNGE on a SELECT'd selection. examine() forces read-only at the
protocol level — the server rejects mutations on the selection regardless
of what bytes the client sends.

Three call sites: initial sync, incremental sync, lifecycle delete-detection."
```

---

## Task 3: Add remaining read-only methods to `IMAPConnection`

`IMAPConnection` currently only exposes `examine()`. The remaining `conn.client.*` call sites (`uid`, `uid_search`, `list`, `capability`, `idle_start`, `idle_done`, `wait_server_push`) bypass the wrapper. We expose each one explicitly so the wrapper becomes the single seam. `uid()` is gated against mutating subcommands.

**Files:**
- Modify: `src/harbor_clerk/mail/imap_client.py`
- Test: `tests/mail/test_imap_client.py`

- [ ] **Step 1: Write the failing test for `uid()` gating**

Add to `tests/mail/test_imap_client.py`:

```python
import pytest

from harbor_clerk.mail.exceptions import ReadOnlyViolation


@pytest.mark.parametrize("verb", ["STORE", "store", "COPY", "MOVE", "EXPUNGE"])
async def test_uid_blocks_mutating_subcommands(_patch_aioimaplib, verb):
    from harbor_clerk.mail.imap_client import IMAPConnection

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    with pytest.raises(ReadOnlyViolation, match=verb.upper()):
        await conn.uid(verb, "1:*", r"+FLAGS (\Seen)")


@pytest.mark.parametrize("verb", ["FETCH", "fetch", "SEARCH"])
async def test_uid_allows_read_subcommands(_patch_aioimaplib, verb, monkeypatch):
    from harbor_clerk.mail.imap_client import IMAPConnection
    from tests.mail.conftest import FakeIMAP

    captured: list[tuple] = []

    async def _uid(self, command, *args):
        captured.append((command, args))
        return "OK", []

    monkeypatch.setattr(FakeIMAP, "uid", _uid)
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    result, _lines = await conn.uid(verb, "1:*")
    assert result == "OK"
    assert captured == [(verb, ("1:*",))]
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/mail/test_imap_client.py::test_uid_blocks_mutating_subcommands tests/mail/test_imap_client.py::test_uid_allows_read_subcommands -v`

Expected: FAIL — `IMAPConnection` has no `uid` method.

- [ ] **Step 3: Add all read-only methods to `IMAPConnection`**

Append to the class in `src/harbor_clerk/mail/imap_client.py`, after the `examine()` method, **and remove the existing `client` property in the same diff**:

```python
    _BLOCKED_UID_VERBS = frozenset({"STORE", "COPY", "MOVE", "EXPUNGE"})

    async def fetch(self, message_set: str, message_parts: str) -> tuple[str, list[bytes]]:
        self._require_logged_in("fetch")
        return await self._client.fetch(message_set, message_parts)

    async def uid(self, command: str, *args: str) -> tuple[str, list[bytes]]:
        """Issue a UID-prefixed command (FETCH, SEARCH only).

        STORE / COPY / MOVE / EXPUNGE are mutating and raise ReadOnlyViolation.
        """
        self._require_logged_in("uid")
        if command.upper() in self._BLOCKED_UID_VERBS:
            raise ReadOnlyViolation(
                f"uid({command!r}) is a mutating IMAP command and is blocked"
            )
        return await self._client.uid(command, *args)

    async def uid_search(self, criteria: str) -> tuple[str, list[bytes]]:
        self._require_logged_in("uid_search")
        return await self._client.uid_search(criteria)

    async def list_mailboxes(self, reference: str, mailbox: str) -> tuple[str, list[bytes]]:
        """IMAP LIST. Renamed from `list` to avoid shadowing the builtin."""
        self._require_logged_in("list_mailboxes")
        return await self._client.list(reference, mailbox)

    async def capability(self) -> tuple[str, list[bytes]]:
        self._require_logged_in("capability")
        return await self._client.capability()

    async def idle_start(self, timeout: float = 0) -> tuple[str, list[bytes]]:
        self._require_logged_in("idle_start")
        return await self._client.idle_start(timeout=timeout)

    async def idle_done(self) -> tuple[str, list[bytes]]:
        self._require_logged_in("idle_done")
        return await self._client.idle_done()

    async def wait_server_push(self, timeout: float | None = None) -> list[bytes]:
        self._require_logged_in("wait_server_push")
        return await self._client.wait_server_push(timeout=timeout)

    def _require_logged_in(self, op: str) -> None:
        if self._client is None or not self._logged_in:
            raise RuntimeError(f"{op}() called before login()")
```

Also add the import at the top:

```python
from harbor_clerk.mail.exceptions import AuthError, ReadOnlyViolation
```

**Do not remove the `client` property in this task** — call sites still depend on it. Removal happens in Task 4 once they're migrated.

- [ ] **Step 4: Run the new tests and verify they pass**

Run: `uv run pytest tests/mail/test_imap_client.py -v`

Expected: all PASS, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/imap_client.py tests/mail/test_imap_client.py
git commit -m "feat(mail): expose read-only IMAP operations on IMAPConnection

Adds fetch / uid / uid_search / list_mailboxes / capability /
idle_start / idle_done / wait_server_push to the wrapper. uid()
inspects its subcommand and raises ReadOnlyViolation on
STORE/COPY/MOVE/EXPUNGE — the only mutating commands reachable
through the polymorphic uid() entry point.

The legacy .client escape hatch is still present and will be
removed in a follow-up once all callers are migrated."
```

---

## Task 4: Migrate remaining `conn.client.*` callers; remove the `.client` escape hatch

**Files:**
- Modify: `src/harbor_clerk/mail/sync.py:113, 123, 200, 212`
- Modify: `src/harbor_clerk/mail/lifecycle.py:46`
- Modify: `src/harbor_clerk/mail/labels.py:87`
- Modify: `src/harbor_clerk/mail/idle.py:21, 48, 52, 67`
- Modify: `src/harbor_clerk/mail/ingest.py:58`
- Modify: `src/harbor_clerk/mail/imap_client.py` (remove `client` property)

- [ ] **Step 1: Migrate `sync.py:113` — `uid_search`**

Change:
```python
    search_result, search_lines = await conn.client.uid_search("ALL")
```
To:
```python
    search_result, search_lines = await conn.uid_search("ALL")
```

- [ ] **Step 2: Migrate `sync.py:123` — `uid("FETCH", ...)`**

Change:
```python
    fetch_result, fetch_lines = await conn.client.uid("FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
```
To:
```python
    fetch_result, fetch_lines = await conn.uid("FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
```

- [ ] **Step 3: Migrate `sync.py:200` and `sync.py:212` identically**

`sync.py:200`:
```python
    search_result, search_lines = await conn.uid_search(search_query)
```

`sync.py:212`:
```python
    fetch_result, fetch_lines = await conn.uid("FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
```

- [ ] **Step 4: Migrate `lifecycle.py:46` — `uid_search`**

```python
    search_result, search_lines = await conn.uid_search("ALL")
```

- [ ] **Step 5: Migrate `labels.py:87` — `list`**

Change:
```python
    result, lines = await conn.client.list('""', '"*"')
```
To:
```python
    result, lines = await conn.list_mailboxes('""', '"*"')
```

- [ ] **Step 6: Migrate `idle.py` (lines 21, 48, 52, 67)**

- Line 21: `result, lines = await conn.capability()`
- Line 48: `await conn.idle_start(timeout=idle_timeout)`
- Line 52: `event = await asyncio.wait_for(conn.wait_server_push(), timeout=idle_timeout)`
- Line 67: `await conn.idle_done()`

- [ ] **Step 7: Migrate `ingest.py:58` — `uid("FETCH", ...)`**

```python
    result, lines = await conn.uid("FETCH", str(uid), "BODY.PEEK[]")
```

- [ ] **Step 8: Verify nothing still references `conn.client.`**

Run: `grep -rn "conn\.client\." src/harbor_clerk/`

Expected output: (no matches).

- [ ] **Step 9: Run the full mail test suite**

Run: `uv run pytest tests/mail/ -v`

Expected: all PASS. If any test still calls `conn.client.*`, fix it in the same diff.

Also run: `grep -rn "conn\.client\." tests/`

Expected: only matches in mocking setup (`monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib...")`); no `conn.client.` in test bodies. If found, migrate them too.

- [ ] **Step 10: Remove the `.client` property from `IMAPConnection`**

Delete lines 76–82 in `src/harbor_clerk/mail/imap_client.py` (the entire `@property def client` block).

The `self._client` attribute stays — only the public `.client` accessor is removed.

- [ ] **Step 11: Write a regression test that the escape hatch is gone**

Add to `tests/mail/test_imap_client.py`:

```python
async def test_client_property_is_not_exposed(_patch_aioimaplib):
    """IMAPConnection must not expose its underlying aioimaplib client.

    The wrapper is the only sanctioned IMAP surface — exposing .client
    would re-open the bypass route this hardening closes.
    """
    from harbor_clerk.mail.imap_client import IMAPConnection

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    assert not hasattr(conn, "client"), (
        "IMAPConnection.client is a known escape hatch; do not re-add it. "
        "Expose read-only operations as explicit methods on the wrapper instead."
    )
```

- [ ] **Step 12: Run all mail tests**

Run: `uv run pytest tests/mail/ -v`

Expected: all PASS.

- [ ] **Step 13: Commit**

```bash
git add -u src/harbor_clerk/mail/ tests/mail/test_imap_client.py
git commit -m "refactor(mail): remove IMAPConnection.client escape hatch

Migrate 11 call sites in sync, lifecycle, labels, idle, ingest off
the bare aioimaplib client onto explicit IMAPConnection methods.
Remove the .client property; add regression test asserting it stays
gone. IMAPConnection is now the sole IMAP surface for the mail
module — adding new operations means adding new explicit methods,
keeping the read-only invariant locally checkable."
```

---

## Task 5: Create `ReadOnlyIMAP4_SSL` subclass

Layer-2 defense. Even if `IMAPConnection` itself grows a new mutating method by mistake, the underlying client will refuse.

**Files:**
- Create: `src/harbor_clerk/mail/readonly_imap.py`
- Test: `tests/mail/test_readonly_imap.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/mail/test_readonly_imap.py`:

```python
"""ReadOnlyIMAP4_SSL must refuse every mutating IMAP command.

This is the layer-2 defense beneath IMAPConnection. If IMAPConnection
itself ever drifts (a new method is added that mutates), or someone
constructs a raw aioimaplib client by mistake, this subclass still
refuses to put mutating bytes on the wire.
"""

from __future__ import annotations

import pytest

from harbor_clerk.mail.exceptions import ReadOnlyViolation
from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL


@pytest.mark.parametrize(
    "method,args",
    [
        ("select", ("INBOX",)),
        ("store", ("1", r"+FLAGS", r"(\Seen)")),
        ("copy", ("1", "Archive")),
        ("move", ("1", "Archive")),
        ("expunge", ()),
        ("append", ("INBOX", None, None, b"From: x\r\n\r\nbody")),
        ("create", ("NewBox",)),
        ("delete", ("OldBox",)),
        ("rename", ("Old", "New")),
        ("subscribe", ("INBOX",)),
        ("unsubscribe", ("INBOX",)),
    ],
)
async def test_mutating_method_raises(method, args):
    client = ReadOnlyIMAP4_SSL.__new__(ReadOnlyIMAP4_SSL)  # bypass network init
    with pytest.raises(ReadOnlyViolation, match=method):
        await getattr(client, method)(*args)


@pytest.mark.parametrize("verb", ["STORE", "store", "COPY", "MOVE", "EXPUNGE"])
async def test_uid_blocks_mutating_verbs(verb):
    client = ReadOnlyIMAP4_SSL.__new__(ReadOnlyIMAP4_SSL)
    with pytest.raises(ReadOnlyViolation, match=verb.upper()):
        await client.uid(verb, "1:*")


def test_block_list_matches_aioimaplib_surface():
    """Catches the case where aioimaplib adds a new mutating method.

    If this test fails, aioimaplib has grown a method that we haven't
    classified as read or mutating — review the new method and either
    expose it via IMAPConnection (read) or add an override here (mutating).
    """
    import aioimaplib

    known_read = {
        "examine", "fetch", "search", "uid", "uid_search", "list", "lsub",
        "status", "noop", "capability", "has_capability", "id", "namespace",
        "get_state", "getquotaroot", "idle", "idle_start", "idle_done",
        "wait_server_push", "has_pending_idle", "stop_wait_server_push",
        "wait_hello_from_server", "login", "logout", "xoauth2", "enable",
        "check", "close", "create_client",
    }
    known_mutating = {
        "select", "store", "copy", "move", "expunge", "append", "create",
        "delete", "rename", "subscribe", "unsubscribe",
    }
    expected = known_read | known_mutating
    actual = {m for m in dir(aioimaplib.IMAP4_SSL) if not m.startswith("_")}
    actual.discard("TIMEOUT_SECONDS")  # class attribute, not a method

    new_methods = actual - expected
    assert not new_methods, (
        f"aioimaplib has new methods that need classification: {new_methods}. "
        f"Add each to known_read or known_mutating (and an override in "
        f"ReadOnlyIMAP4_SSL if mutating)."
    )
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/mail/test_readonly_imap.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'harbor_clerk.mail.readonly_imap'`.

- [ ] **Step 3: Implement `ReadOnlyIMAP4_SSL`**

Create `src/harbor_clerk/mail/readonly_imap.py`:

```python
"""Read-only subclass of aioimaplib.IMAP4_SSL.

Layer-2 defense beneath IMAPConnection. The wrapper exposes a curated
read-only surface and removes the .client escape hatch; this subclass
provides a second, lower-level guarantee: every mutating IMAP command
raises ReadOnlyViolation before any bytes go on the wire.

If both layers fail, layer 1 (the IMAP protocol's EXAMINE selection)
still rejects mutations at the server.
"""

from __future__ import annotations

import aioimaplib

from harbor_clerk.mail.exceptions import ReadOnlyViolation

_BLOCKED_UID_VERBS = frozenset({"STORE", "COPY", "MOVE", "EXPUNGE"})


class ReadOnlyIMAP4_SSL(aioimaplib.IMAP4_SSL):
    """aioimaplib.IMAP4_SSL with all mutating methods replaced by raisers."""

    async def select(self, *_args, **_kwargs):
        raise ReadOnlyViolation(
            "select() opens the mailbox read-write; use examine() instead"
        )

    async def store(self, *_args, **_kwargs):
        raise ReadOnlyViolation("store() mutates message flags")

    async def copy(self, *_args, **_kwargs):
        raise ReadOnlyViolation("copy() writes to the target mailbox")

    async def move(self, *_args, **_kwargs):
        raise ReadOnlyViolation("move() removes from the source mailbox")

    async def expunge(self, *_args, **_kwargs):
        raise ReadOnlyViolation("expunge() permanently removes messages")

    async def append(self, *_args, **_kwargs):
        raise ReadOnlyViolation("append() adds a message to a mailbox")

    async def create(self, *_args, **_kwargs):
        raise ReadOnlyViolation("create() creates a new mailbox")

    async def delete(self, *_args, **_kwargs):
        raise ReadOnlyViolation("delete() removes a mailbox")

    async def rename(self, *_args, **_kwargs):
        raise ReadOnlyViolation("rename() renames a mailbox")

    async def subscribe(self, *_args, **_kwargs):
        raise ReadOnlyViolation("subscribe() mutates the user's subscription list")

    async def unsubscribe(self, *_args, **_kwargs):
        raise ReadOnlyViolation("unsubscribe() mutates the user's subscription list")

    async def uid(self, command, *args, **kwargs):
        if command.upper() in _BLOCKED_UID_VERBS:
            raise ReadOnlyViolation(
                f"uid({command!r}) is a mutating IMAP command"
            )
        return await super().uid(command, *args, **kwargs)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `uv run pytest tests/mail/test_readonly_imap.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/readonly_imap.py tests/mail/test_readonly_imap.py
git commit -m "feat(mail): add ReadOnlyIMAP4_SSL subclass

Layer-2 defense: subclass of aioimaplib.IMAP4_SSL that raises
ReadOnlyViolation on every mutating IMAP command (SELECT, STORE,
COPY, MOVE, EXPUNGE, APPEND, CREATE, DELETE, RENAME,
SUBSCRIBE, UNSUBSCRIBE) and on UID-prefixed mutating verbs.

Not wired in yet — IMAPConnection still constructs the bare
aioimaplib client. Hookup is the next task."
```

---

## Task 6: Wire `ReadOnlyIMAP4_SSL` into `IMAPConnection.connect()`

**Files:**
- Modify: `src/harbor_clerk/mail/imap_client.py`
- Test: `tests/mail/test_readonly_imap.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/mail/test_readonly_imap.py`:

```python
async def test_imap_connection_uses_readonly_subclass(monkeypatch):
    """IMAPConnection.connect() must instantiate ReadOnlyIMAP4_SSL.

    If a future refactor drops back to bare aioimaplib.IMAP4_SSL the
    layer-2 defense disappears silently — this test pins it.
    """
    from harbor_clerk.mail import imap_client
    from harbor_clerk.mail.imap_client import IMAPConnection
    from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL

    captured: list[type] = []
    original = imap_client.ReadOnlyIMAP4_SSL

    class _Probe(original):
        def __init__(self, *a, **kw):
            captured.append(type(self))

        async def wait_hello_from_server(self):
            pass

    monkeypatch.setattr(imap_client, "ReadOnlyIMAP4_SSL", _Probe)
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()

    assert captured, "ReadOnlyIMAP4_SSL was not instantiated"
    assert issubclass(captured[0], ReadOnlyIMAP4_SSL)
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `uv run pytest tests/mail/test_readonly_imap.py::test_imap_connection_uses_readonly_subclass -v`

Expected: FAIL — `imap_client` has no attribute `ReadOnlyIMAP4_SSL`.

- [ ] **Step 3: Wire the subclass into `IMAPConnection.connect()`**

In `src/harbor_clerk/mail/imap_client.py`:

Add import near the top:
```python
from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL
```

Change `connect()`:
```python
    async def connect(self) -> None:
        """Open TCP connection and complete the IMAP server greeting."""
        self._client = ReadOnlyIMAP4_SSL(host=self.host, port=self.port)
        await self._client.wait_hello_from_server()
```

- [ ] **Step 4: Update existing test patches that target `aioimaplib.IMAP4_SSL`**

The mail-test conftest patches `harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL`. With the new code, we instantiate `ReadOnlyIMAP4_SSL`, not `aioimaplib.IMAP4_SSL` directly. Update the patches.

In every test file using the pattern (`tests/mail/test_imap_client.py`, `tests/mail/test_sync_initial.py`, `tests/mail/test_sync_incremental.py`, `tests/mail/test_sync_uidvalidity.py`, `tests/mail/test_labels.py`, `tests/mail/test_lifecycle.py`, `tests/mail/test_ingest.py`, `tests/mail/test_mail_observer.py`, `tests/mail/test_idle_polling.py`, `tests/api/test_mail_routes.py`):

Change:
```python
monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
```
To:
```python
monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
```

Run: `grep -rln "imap_client.aioimaplib.IMAP4_SSL" tests/`

Apply the replacement in every file the grep returns.

- [ ] **Step 5: Run all mail tests**

Run: `uv run pytest tests/mail/ tests/api/test_mail_routes.py -v`

Expected: all PASS, including the new `test_imap_connection_uses_readonly_subclass`.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/mail/imap_client.py tests/
git commit -m "feat(mail): wire ReadOnlyIMAP4_SSL into IMAPConnection.connect

IMAPConnection now instantiates the read-only subclass for every
mail connection. The mutating-command block is now enforced before
bytes reach the network, in addition to the wrapper's allow-list
and the server's EXAMINE-mode rejection.

Test patches updated to target imap_client.ReadOnlyIMAP4_SSL."
```

---

## Task 7: Tighten the `FakeIMAP` test fixture

The fake currently exposes a permissive `select()` and has no mutating-method stubs. Production-side hardening (Tasks 1–6) is partly negated if unit tests pass against a fake that allows what we forbid. We update the fake to mirror the real-world invariants.

**Files:**
- Modify: `tests/mail/conftest.py`

- [ ] **Step 1: Update `FakeIMAP` to mirror `ReadOnlyIMAP4_SSL` semantics**

In `tests/mail/conftest.py`, modify the `FakeIMAP` class:

1. Replace the existing `select()` method with `examine()`. Reuse the response-staging:

```python
    @classmethod
    def set_examine_response(cls, result: str, lines: list[bytes]) -> None:
        cls._examine_response = _Response(result, lines)

    async def examine(self, mailbox: str):
        resp = self._examine_response or _Response("OK", [b"* OK [READ-ONLY]"])
        return resp.result, resp.lines
```

Add `_examine_response: _Response | None = None` to the class attributes and `cls._examine_response = None` to `reset()`. Keep the existing `set_select_response`/`_select_response` for one release as a soft-migration helper — but have `select()` raise:

```python
    async def select(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: select() is forbidden — use examine()")
```

2. Add raising stubs for all mutating methods (matching `ReadOnlyIMAP4_SSL`):

```python
    async def store(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: store() is forbidden")

    async def copy(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: copy() is forbidden")

    async def move(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: move() is forbidden")

    async def expunge(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: expunge() is forbidden")

    async def append(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: append() is forbidden")

    async def create(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: create() is forbidden")

    async def delete(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: delete() is forbidden")

    async def rename(self, *_args, **_kwargs):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        raise ReadOnlyViolation("FakeIMAP: rename() is forbidden")
```

3. Add `uid()` gating identical to `ReadOnlyIMAP4_SSL`:

```python
    async def uid(self, command: str, *args):
        from harbor_clerk.mail.exceptions import ReadOnlyViolation
        if command.upper() in {"STORE", "COPY", "MOVE", "EXPUNGE"}:
            raise ReadOnlyViolation(f"FakeIMAP: uid({command!r}) is forbidden")
        # Existing behavior:
        if command.upper() == "FETCH":
            resp = self._uid_fetch_response or _Response("OK", [])
            return resp.result, resp.lines
        return "OK", []
```

- [ ] **Step 2: Update tests that staged `set_select_response` to use `set_examine_response`**

Run: `grep -rln "set_select_response" tests/`

Replace each call site with `set_examine_response` (semantics unchanged — same response staging, different verb).

- [ ] **Step 3: Run all mail tests**

Run: `uv run pytest tests/mail/ tests/api/test_mail_routes.py -v`

Expected: all PASS. If any test fails because production code is calling a mutating method that we missed in earlier tasks, **stop and fix the production code** — do not weaken the fake. That's the whole point of this task.

- [ ] **Step 4: Commit**

```bash
git add tests/mail/conftest.py tests/
git commit -m "test(mail): FakeIMAP enforces same read-only invariant as production

FakeIMAP previously exposed permissive select() / store() / etc.
stubs that returned OK without complaint. This let unit tests
silently pass against code paths that would have been rejected in
production by ReadOnlyIMAP4_SSL.

Now the fake raises ReadOnlyViolation on every mutating method
and gates uid() against mutating subcommands — symmetric with
the production wrapper. Failure here means production code is
attempting a mutating IMAP call and needs to be fixed, not the
fake weakened."
```

---

## Task 8: Add `imap_command_log` migration

**Files:**
- Create: `alembic/versions/0022_imap_command_log.py`

- [ ] **Step 1: Verify the current head revision is 0021**

Run: `uv run alembic heads`

Expected: `0021 (head)`.

- [ ] **Step 2: Create the migration**

Create `alembic/versions/0022_imap_command_log.py`:

```python
"""Create imap_command_log table.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "imap_command_log",
        sa.Column(
            "log_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label_path", sa.Text(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("args_redacted", sa.Text(), nullable=True),
        sa.Column("response_status", sa.Text(), nullable=False),
        sa.Column("response_bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_imap_command_log_account_time",
        "imap_command_log",
        ["account_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_imap_command_log_created",
        "imap_command_log",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_imap_command_log_created", table_name="imap_command_log")
    op.drop_index("ix_imap_command_log_account_time", table_name="imap_command_log")
    op.drop_table("imap_command_log")
```

- [ ] **Step 3: Apply the migration to a test DB and verify**

Run: `uv run alembic upgrade head`

Expected: migration applies cleanly, ends at `0022 (head)`.

Verify table shape:
```bash
psql -h localhost -p 5433 -U harbor_clerk -d harbor_clerk -c "\d imap_command_log"
```

Expected: columns and indexes match the migration.

- [ ] **Step 4: Test downgrade**

Run: `uv run alembic downgrade 0021 && uv run alembic upgrade head`

Expected: both succeed; table is dropped then recreated identically.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0022_imap_command_log.py
git commit -m "feat(db): add imap_command_log table

Per-account audit trail of every IMAP command issued by the
sync engine. account_id FK to mail_accounts (CASCADE so removing
an account also drops its audit history). args_redacted stores
the formatted command arguments with LOGIN / XOAUTH2 passwords
masked. response_bytes / duration_ms give incident-time signal
without retaining message bodies."
```

---

## Task 9: Add `ImapCommandLog` SQLAlchemy model

**Files:**
- Create: `src/harbor_clerk/models/imap_command_log.py`
- Modify: `src/harbor_clerk/models/__init__.py`
- Test: `tests/mail/test_imap_audit.py`

- [ ] **Step 1: Write the failing test for the model**

Create `tests/mail/test_imap_audit.py`:

```python
"""Audit-log model, redaction helper, and reaper tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


async def test_imap_command_log_round_trips(db_session, mail_account):
    """Insert a row and read it back."""
    from harbor_clerk.models import ImapCommandLog

    row = ImapCommandLog(
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args_redacted="INBOX",
        response_status="OK",
        response_bytes=42,
        duration_ms=17,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.log_id is not None
    assert row.created_at is not None
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `uv run pytest tests/mail/test_imap_audit.py::test_imap_command_log_round_trips -v`

Expected: FAIL — `ImportError: cannot import name 'ImapCommandLog'`.

- [ ] **Step 3: Create the model**

Create `src/harbor_clerk/models/imap_command_log.py`:

```python
"""SQLAlchemy model for the imap_command_log table."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from harbor_clerk.models.base import Base


class ImapCommandLog(Base):
    """Audit row recording one IMAP command issued by the sync engine."""

    __tablename__ = "imap_command_log"

    log_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    label_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    command: Mapped[str] = mapped_column(Text(), nullable=False)
    args_redacted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    response_status: Mapped[str] = mapped_column(Text(), nullable=False)
    response_bytes: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0")
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0")
    )
    error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
```

- [ ] **Step 4: Export from `src/harbor_clerk/models/__init__.py`**

Find where other models are exported (open the file, find the existing pattern), and add:

```python
from harbor_clerk.models.imap_command_log import ImapCommandLog  # noqa: F401
```

Also add `"ImapCommandLog"` to `__all__` if present.

- [ ] **Step 5: Run the test and verify it passes**

Run: `uv run pytest tests/mail/test_imap_audit.py::test_imap_command_log_round_trips -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/models/imap_command_log.py src/harbor_clerk/models/__init__.py tests/mail/test_imap_audit.py
git commit -m "feat(models): ImapCommandLog SQLAlchemy model"
```

---

## Task 10: Audit logger with redaction

**Files:**
- Create: `src/harbor_clerk/mail/audit.py`
- Test: `tests/mail/test_imap_audit.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/mail/test_imap_audit.py`:

```python
def test_redact_login_masks_password():
    """LOGIN args contain the user's mail password in cleartext;
    storing them verbatim is a credential leak waiting to happen."""
    from harbor_clerk.mail.audit import redact_imap_args

    redacted = redact_imap_args("LOGIN", ("user@example.com", "s3cret-app-password"))
    assert "s3cret-app-password" not in redacted
    assert "user@example.com" in redacted
    assert "[redacted]" in redacted


def test_redact_xoauth2_masks_token():
    from harbor_clerk.mail.audit import redact_imap_args

    redacted = redact_imap_args(
        "XOAUTH2",
        ("user@example.com", "ya29.long-bearer-token-value"),
    )
    assert "ya29.long-bearer-token-value" not in redacted
    assert "[redacted]" in redacted


def test_redact_fetch_records_shape_not_body():
    """FETCH responses contain full message bodies. We log the request
    args (which are bounded) but never the response body."""
    from harbor_clerk.mail.audit import redact_imap_args

    redacted = redact_imap_args("FETCH", ("1:5", "(BODY.PEEK[])"))
    assert "1:5" in redacted
    assert "BODY.PEEK[]" in redacted


def test_redact_passthrough_for_safe_commands():
    from harbor_clerk.mail.audit import redact_imap_args

    assert redact_imap_args("EXAMINE", ("INBOX",)) == "INBOX"
    assert redact_imap_args("CAPABILITY", ()) == ""


async def test_log_imap_command_writes_row(db_session, mail_account):
    from harbor_clerk.mail.audit import log_imap_command
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select

    await log_imap_command(
        db_session,
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args=("INBOX",),
        response_status="OK",
        response_bytes=42,
        duration_ms=17,
        error=None,
    )
    await db_session.flush()
    row = (await db_session.execute(select(ImapCommandLog))).scalar_one()
    assert row.command == "EXAMINE"
    assert row.args_redacted == "INBOX"
    assert row.response_status == "OK"
    assert row.error is None


async def test_log_imap_command_redacts_login(db_session, mail_account):
    from harbor_clerk.mail.audit import log_imap_command
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select

    await log_imap_command(
        db_session,
        account_id=mail_account.account_id,
        label_path=None,
        command="LOGIN",
        args=("user@example.com", "s3cret"),
        response_status="OK",
        response_bytes=0,
        duration_ms=12,
        error=None,
    )
    await db_session.flush()
    row = (await db_session.execute(select(ImapCommandLog))).scalar_one()
    assert "s3cret" not in (row.args_redacted or "")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/mail/test_imap_audit.py -v`

Expected: 5 new tests FAIL with `ModuleNotFoundError: No module named 'harbor_clerk.mail.audit'`. Existing `test_imap_command_log_round_trips` still PASSES.

- [ ] **Step 3: Implement the audit helpers**

Create `src/harbor_clerk/mail/audit.py`:

```python
"""Audit logging for IMAP commands.

Every IMAP command issued by the sync engine is recorded in the
imap_command_log table — verb, redacted args, response status,
response size, duration. LOGIN and XOAUTH2 credentials are masked
before storage; response bodies are never persisted (only the byte
count).

A periodic reaper drops rows older than 30 days.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.models import ImapCommandLog

# Commands whose arguments contain credentials.
_CREDENTIAL_COMMANDS = frozenset({"LOGIN", "XOAUTH2"})

# How long to retain audit rows. Incident-driven access pattern — 30
# days is enough for most postmortems; users worried about a longer
# window can raise this.
RETENTION_DAYS = 30


def redact_imap_args(command: str, args: tuple) -> str:
    """Format command arguments for storage, masking credentials.

    For LOGIN and XOAUTH2 the password / token (last positional arg)
    is replaced with the literal '[redacted]'. All other commands
    are passed through with simple space-joined string formatting.
    """
    cmd = command.upper()
    if cmd in _CREDENTIAL_COMMANDS and args:
        # The credential is the last arg by IMAP convention:
        # LOGIN <username> <password>; XOAUTH2 <username> <token>.
        safe = list(args[:-1]) + ["[redacted]"]
        return " ".join(str(a) for a in safe)
    return " ".join(str(a) for a in args)


async def log_imap_command(
    session: AsyncSession,
    *,
    account_id: UUID,
    label_path: str | None,
    command: str,
    args: tuple,
    response_status: str,
    response_bytes: int,
    duration_ms: int,
    error: str | None,
) -> None:
    """Insert one audit row. Caller is responsible for the surrounding
    transaction (flush/commit). Errors here must not break the IMAP
    operation that's being audited — callers should wrap and log."""
    session.add(
        ImapCommandLog(
            account_id=account_id,
            label_path=label_path,
            command=command.upper(),
            args_redacted=redact_imap_args(command, args),
            response_status=response_status,
            response_bytes=response_bytes,
            duration_ms=duration_ms,
            error=error,
        )
    )


async def reap_old_imap_command_logs(
    session: AsyncSession, *, retention_days: int = RETENTION_DAYS
) -> int:
    """Delete rows older than retention_days. Returns the number of
    rows deleted (for logging / metrics)."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await session.execute(
        delete(ImapCommandLog).where(ImapCommandLog.created_at < cutoff)
    )
    return result.rowcount or 0
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `uv run pytest tests/mail/test_imap_audit.py -v`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/mail/audit.py tests/mail/test_imap_audit.py
git commit -m "feat(mail): IMAP audit logger with credential redaction

log_imap_command() persists one row per IMAP command issued.
redact_imap_args() masks the password in LOGIN args and the
bearer token in XOAUTH2 args — both contain credentials that
must never be stored verbatim.

reap_old_imap_command_logs() drops rows older than 30 days;
wired into the periodic-tasks loop in the next task."
```

---

## Task 11: Wire the audit logger into `IMAPConnection`

The wrapper now becomes the single point that records every IMAP command. Each method we added in Task 3 gets the same boilerplate: capture start time, issue command, record result.

**Files:**
- Modify: `src/harbor_clerk/mail/imap_client.py`
- Test: `tests/mail/test_imap_audit.py` (extend)

- [ ] **Step 1: Write the failing test — examine() records a log row**

Append to `tests/mail/test_imap_audit.py`:

```python
async def test_examine_records_audit_row(
    _patch_aioimaplib, db_session, mail_account, monkeypatch
):
    """Calling IMAPConnection.examine() must persist one ImapCommandLog row."""
    from harbor_clerk.mail.imap_client import IMAPConnection
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select
    from tests.mail.conftest import FakeIMAP

    async def _examine(self, mailbox):
        return "OK", [b"* OK [READ-ONLY]"]

    monkeypatch.setattr(FakeIMAP, "examine", _examine, raising=False)

    conn = IMAPConnection(
        host="h", port=993, username="u", password="p",
        audit_session=db_session, account_id=mail_account.account_id,
    )
    await conn.connect()
    await conn.login()
    await conn.examine("INBOX")
    await db_session.flush()

    rows = (await db_session.execute(select(ImapCommandLog).order_by(ImapCommandLog.created_at))).scalars().all()
    # login + examine
    assert [r.command for r in rows] == ["LOGIN", "EXAMINE"]
    assert rows[1].label_path == "INBOX" or rows[1].args_redacted == "INBOX"
    assert rows[0].args_redacted and "[redacted]" in rows[0].args_redacted
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `uv run pytest tests/mail/test_imap_audit.py::test_examine_records_audit_row -v`

Expected: FAIL — `IMAPConnection.__init__` takes no `audit_session` parameter.

- [ ] **Step 3: Extend `IMAPConnection` constructor and wrap methods**

In `src/harbor_clerk/mail/imap_client.py`:

1. Add imports:
```python
import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.audit import log_imap_command
```

2. Extend the constructor:
```python
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        audit_session: AsyncSession | None = None,
        account_id: UUID | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._client: Any | None = None
        self._logged_in = False
        self._audit_session = audit_session
        self._account_id = account_id
        self._current_label: str | None = None
```

3. Add a helper `_audit()` method on the class:

```python
    async def _audit(
        self,
        command: str,
        args: tuple,
        result: tuple[str, list[bytes]] | None,
        duration_ms: int,
        error: str | None,
    ) -> None:
        """Write one audit row. Failure must not break the IMAP call."""
        if self._audit_session is None or self._account_id is None:
            return
        if result is None:
            status, lines = "ERROR", []
        else:
            status, lines = result
        response_bytes = sum(len(line) for line in lines) if lines else 0
        try:
            await log_imap_command(
                self._audit_session,
                account_id=self._account_id,
                label_path=self._current_label,
                command=command,
                args=args,
                response_status=status,
                response_bytes=response_bytes,
                duration_ms=duration_ms,
                error=error,
            )
        except Exception as exc:  # never let audit break the IMAP op
            logger.warning("audit log failed for %s: %s", command, exc)
```

4. Wrap each public method. Example pattern (apply to `login`, `examine`, `fetch`, `uid`, `uid_search`, `list_mailboxes`, `capability`, `idle_start`, `idle_done`):

```python
    async def examine(self, mailbox: str) -> tuple[str, list[bytes]]:
        self._require_logged_in("examine")
        start = time.perf_counter()
        err: str | None = None
        try:
            result = await self._client.examine(mailbox)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("EXAMINE", (mailbox,), None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        self._current_label = mailbox  # subsequent fetch/uid calls inherit this
        await self._audit("EXAMINE", (mailbox,), result, duration_ms, None)
        return result
```

Apply the same pattern to every public method. For `login()`, also record:
- `command="LOGIN"`
- `args=(self.username, self._password)` — the redactor masks the password before storage
- DO NOT set `_current_label` (login is not a mailbox op)

Special case: `wait_server_push` — auditing every IDLE tick is too noisy. **Skip auditing this method** (return its result directly). Document the carve-out inline:

```python
    async def wait_server_push(self, timeout: float | None = None) -> list[bytes]:
        """NOT AUDITED — IDLE pushes fire continuously and would flood the log.
        The preceding idle_start / following idle_done are audited."""
        self._require_logged_in("wait_server_push")
        return await self._client.wait_server_push(timeout=timeout)
```

- [ ] **Step 4: Update `MailObserver` to pass audit_session and account_id when constructing connections**

Find every `IMAPConnection(...)` call site:

Run: `grep -rn "IMAPConnection(" src/harbor_clerk/`

For each construction in production code (typically `src/harbor_clerk/watcher/mail_observer.py`), pass through `audit_session=<async session>` and `account_id=<account.account_id>`. The `audit_session` needs a way to flush separately from the main transaction — easiest approach: each `_run_label` task gets its own short-lived session for audit writes, distinct from the sync engine's transactional session. Document this in the code:

```python
# Use a dedicated audit session so audit writes commit independently
# of the sync transaction. If the sync rolls back, we still keep the
# log of what was attempted.
```

If there's significant complexity here (e.g., session lifecycle is non-trivial), call it out as a follow-up: production may need a small session-factory helper. For this plan: introduce a minimal helper in `src/harbor_clerk/mail/audit.py`:

```python
from contextlib import asynccontextmanager

from harbor_clerk.db import AsyncSessionLocal  # whatever the existing factory is named


@asynccontextmanager
async def audit_session_scope():
    """Short-lived session for IMAP audit writes. Commits on exit."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

(Inspect the existing factory name with: `grep -rn "AsyncSessionLocal\|async_session_factory\|sessionmaker" src/harbor_clerk/db.py src/harbor_clerk/database.py 2>/dev/null`. Use whatever the project uses.)

- [ ] **Step 5: Run all mail tests**

Run: `uv run pytest tests/mail/ tests/api/test_mail_routes.py -v`

Expected: all PASS, including the new `test_examine_records_audit_row`.

- [ ] **Step 6: Run the full test suite as a regression check**

Run: `uv run pytest`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add -u src/harbor_clerk/ tests/
git commit -m "feat(mail): record every IMAP command to imap_command_log

IMAPConnection wraps each public method with audit instrumentation:
captures duration, response size, response status, and on failure,
the exception repr. LOGIN is recorded with the password redacted.
Response bodies are never persisted — only their byte count.

wait_server_push (the IDLE event firehose) is intentionally not
audited to keep log volume manageable; the surrounding
idle_start / idle_done bracket each IDLE session.

MailObserver wires a dedicated short-lived audit session per
label task so audit writes commit independently of the sync
transaction."
```

---

## Task 12: Schedule the 30-day reaper

**Files:**
- Modify: `src/harbor_clerk/app.py` (or wherever the existing periodic-tasks loop lives)
- Test: `tests/mail/test_imap_audit.py` (extend)

- [ ] **Step 1: Find the existing periodic-task scheduling pattern**

Run: `grep -rn "session_reaper_loop\|_periodic_\|asyncio.create_task" src/harbor_clerk/app.py`

Identify how `_session_reaper_loop` (mentioned in the watcher state map) is scheduled. The IMAP-log reaper will follow the same pattern.

- [ ] **Step 2: Write the failing test for the reaper**

Append to `tests/mail/test_imap_audit.py`:

```python
async def test_reap_old_imap_command_logs(db_session, mail_account):
    """Rows older than retention_days are deleted; younger rows survive."""
    from datetime import UTC, datetime, timedelta

    from harbor_clerk.mail.audit import reap_old_imap_command_logs
    from harbor_clerk.models import ImapCommandLog
    from sqlalchemy import select

    now = datetime.now(UTC)
    old = ImapCommandLog(
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args_redacted="INBOX",
        response_status="OK",
        response_bytes=0,
        duration_ms=0,
        created_at=now - timedelta(days=45),
    )
    young = ImapCommandLog(
        account_id=mail_account.account_id,
        label_path="INBOX",
        command="EXAMINE",
        args_redacted="INBOX",
        response_status="OK",
        response_bytes=0,
        duration_ms=0,
        created_at=now - timedelta(days=5),
    )
    db_session.add_all([old, young])
    await db_session.flush()

    deleted = await reap_old_imap_command_logs(db_session, retention_days=30)
    await db_session.flush()
    remaining = (await db_session.execute(select(ImapCommandLog))).scalars().all()

    assert deleted == 1
    assert {r.log_id for r in remaining} == {young.log_id}
```

- [ ] **Step 3: Run the test and verify it passes**

`reap_old_imap_command_logs` already exists from Task 10. The test exists to pin the contract.

Run: `uv run pytest tests/mail/test_imap_audit.py::test_reap_old_imap_command_logs -v`

Expected: PASS.

- [ ] **Step 4: Wire the reaper into the periodic-tasks loop**

In `src/harbor_clerk/app.py`, in the same place that schedules `_session_reaper_loop`, add a sibling task. The exact pattern depends on what the file looks like (Step 1 output) — likely something like:

```python
from harbor_clerk.mail.audit import audit_session_scope, reap_old_imap_command_logs


async def _imap_audit_reaper_loop():
    """Reap imap_command_log rows older than 30 days, hourly."""
    while True:
        try:
            async with audit_session_scope() as session:
                deleted = await reap_old_imap_command_logs(session)
                if deleted:
                    logger.info("imap_command_log reaper: deleted %d rows", deleted)
        except Exception:
            logger.exception("imap_command_log reaper failed")
        await asyncio.sleep(3600)  # hourly
```

Then in the app startup section, add `asyncio.create_task(_imap_audit_reaper_loop())` alongside the existing reaper.

- [ ] **Step 5: Run the test suite as a smoke check**

Run: `uv run pytest -x`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -u src/harbor_clerk/app.py tests/mail/test_imap_audit.py
git commit -m "feat(mail): schedule 30-day reaper for imap_command_log

Hourly background task drops audit rows older than 30 days. The
IMAP audit is an incident-driven artifact — admins read it only
after something goes wrong — so a month of history is enough.
Volume-conscious admins worried about high-traffic accounts can
shorten retention by adjusting RETENTION_DAYS."
```

---

## Task 13: Static no-mutation assertion test

A belt-and-suspenders check: walk the `mail/` module at import time and assert that no production code calls a mutating method by name. This catches a regression even if the wrapper, subclass, and FakeIMAP all somehow miss it.

**Files:**
- Create: `tests/mail/test_no_mutation_static.py`

- [ ] **Step 1: Write the test**

Create `tests/mail/test_no_mutation_static.py`:

```python
"""Static guard: no production code in src/harbor_clerk/mail/ may call
an IMAP mutating method by name.

This complements the runtime defenses (ReadOnlyIMAP4_SSL, FakeIMAP)
by catching the case where a developer reaches around the wrapper to
construct or grab a raw aioimaplib client.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Method names whose invocation on ANY object in mail/ is forbidden.
# We accept the over-broad match — there's no benign reason for a
# Python module under harbor_clerk.mail to call .store / .copy / .move
# / .expunge / .append on any object. (.append on lists is fine but
# the cost of renaming the helper if it ever collides is trivial.)
MUTATING_NAMES = frozenset({
    "store", "copy_messages", "move_messages", "expunge",
    "imap_append", "uid_store", "uid_copy", "uid_move", "uid_expunge",
})

# Bare select() is forbidden — examine() is the sanctioned form.
# We allow conn.examine, FakeIMAP.examine; we don't allow .select()
# anywhere in production code.
FORBIDDEN_BARE = frozenset({"select"})

MAIL_DIR = Path(__file__).resolve().parents[2] / "src" / "harbor_clerk" / "mail"


def _scan(source: str) -> list[tuple[str, int]]:
    """Return (method_name, line) for every Attribute call in source
    whose method name is in MUTATING_NAMES or FORBIDDEN_BARE."""
    tree = ast.parse(source)
    violations: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in MUTATING_NAMES or name in FORBIDDEN_BARE:
                violations.append((name, node.lineno))
    return violations


@pytest.mark.parametrize("path", sorted(MAIL_DIR.rglob("*.py")))
def test_no_mutating_imap_calls(path):
    """No mail module may call a mutating IMAP method or bare select()."""
    # readonly_imap.py defines overrides; their bodies call the parent
    # method via super() — that's the one sanctioned exception.
    if path.name == "readonly_imap.py":
        pytest.skip("subclass is allowed to reference parent mutating methods")
    source = path.read_text(encoding="utf-8")
    violations = _scan(source)
    assert not violations, (
        f"{path}: found forbidden IMAP calls: {violations}. "
        f"Use IMAPConnection methods (examine, fetch, uid, ...) instead."
    )
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/mail/test_no_mutation_static.py -v`

Expected: all PASS. If anything fails, the offending file/line is reported — fix by routing through the wrapper.

- [ ] **Step 3: Commit**

```bash
git add tests/mail/test_no_mutation_static.py
git commit -m "test(mail): static guard against IMAP mutating calls

AST scan of src/harbor_clerk/mail/*.py rejecting calls to
.store / .expunge / .copy_messages / .move_messages / etc.,
and bare .select() (use .examine() instead). Catches regressions
where production code reaches around IMAPConnection to grab a
raw aioimaplib client and call a mutating method directly."
```

---

## Task 14: Integration test — no-mutation invariant against real Dovecot

**Files:**
- Modify: `tests/integration/test_mail_e2e_dovecot.py`

- [ ] **Step 1: Read the existing integration test to learn its fixtures and setup**

Run: `cat tests/integration/test_mail_e2e_dovecot.py`

Note: how it spins up Dovecot, how it authenticates, what seeded state exists.

- [ ] **Step 2: Add a snapshot-diff invariant test**

Append (or insert near similar tests) in `tests/integration/test_mail_e2e_dovecot.py`:

```python
@pytest.mark.integration
async def test_no_mutation_invariant_against_dovecot(
    dovecot_account, seeded_inbox, db_session
):
    """End-to-end invariant: a full mail-ingest cycle must not mutate
    server state.

    Strategy:
      1. Snapshot the mailbox via an INDEPENDENT raw IMAP client (NOT
         our wrapper) — capture: message count, per-UID flags, folder
         list, UIDVALIDITY, UIDNEXT.
      2. Run Harbor Clerk's full ingestion path: connect → examine →
         uid_search → uid("FETCH", ...) → idle for 3s → logout.
      3. Snapshot again the same way.
      4. Diff: assert equality on every snapshotted field.

    If anything differs, the suite has detected a mutation that
    layers 1–3 missed.
    """
    import aioimaplib

    from harbor_clerk.mail.imap_client import IMAPConnection

    async def snapshot() -> dict:
        raw = aioimaplib.IMAP4_SSL(host=dovecot_account.imap_host, port=dovecot_account.imap_port)
        await raw.wait_hello_from_server()
        await raw.login(dovecot_account.imap_username, dovecot_account.password_plaintext)
        # List folders.
        _list_status, list_lines = await raw.list('""', '"*"')
        # EXAMINE INBOX (not SELECT — we don't want our snapshot to mutate either).
        _exam_status, exam_lines = await raw.examine("INBOX")
        # Find UIDVALIDITY and UIDNEXT.
        uidvalidity = None
        uidnext = None
        for line in exam_lines:
            if b"UIDVALIDITY" in line:
                uidvalidity = line
            if b"UIDNEXT" in line:
                uidnext = line
        # Per-UID flags.
        _search_status, search_lines = await raw.uid_search("ALL")
        _fetch_status, fetch_lines = await raw.uid("FETCH", "1:*", "(FLAGS)")
        await raw.logout()
        return {
            "list": list_lines,
            "uidvalidity": uidvalidity,
            "uidnext": uidnext,
            "search": search_lines,
            "fetch_flags": fetch_lines,
        }

    before = await snapshot()

    # Run the full ingestion path under test.
    conn = IMAPConnection(
        host=dovecot_account.imap_host,
        port=dovecot_account.imap_port,
        username=dovecot_account.imap_username,
        password=dovecot_account.password_plaintext,
        audit_session=db_session,
        account_id=dovecot_account.account_id,
    )
    await conn.connect()
    await conn.login()
    await conn.examine("INBOX")
    await conn.uid_search("ALL")
    await conn.uid("FETCH", "1:*", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    # Brief IDLE to exercise the push path.
    await conn.idle_start(timeout=3)
    try:
        await asyncio.wait_for(conn.wait_server_push(), timeout=3)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    await conn.idle_done()
    await conn.logout()

    after = await snapshot()

    assert before == after, (
        f"IMAP server state changed during ingestion:\n"
        f"  before: {before}\n"
        f"  after:  {after}"
    )
```

- [ ] **Step 3: Run the integration test against a local Dovecot**

(Skip this step if the integration test environment isn't available locally — note that the test is marked `@pytest.mark.integration` and will be skipped by the default CI run.)

If running locally with Dovecot up:

```bash
DOVECOT_TEST_HOST=localhost DOVECOT_TEST_PORT=1143 uv run pytest tests/integration/test_mail_e2e_dovecot.py::test_no_mutation_invariant_against_dovecot -v -m integration
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_mail_e2e_dovecot.py
git commit -m "test(integration): no-mutation invariant against real Dovecot

Snapshot the server state via an independent raw IMAP client
before and after a full Harbor Clerk ingestion cycle (connect,
examine, search, fetch, idle, logout). Diff for any change in
folder list, UIDVALIDITY, UIDNEXT, message UIDs, or per-message
flags. End-to-end proof that no layer of our code mutates the
server."
```

---

## Task 15: Per-provider documentation

**Files:**
- Create: `docs/imap-readonly.md`

- [ ] **Step 1: Write the doc**

Create `docs/imap-readonly.md`:

```markdown
# IMAP Read-Only Safeguards

Harbor Clerk's mail ingestion is strictly read-only. This document
explains the layers of protection in place, what they guarantee, and
what self-hosters can do to add a further server-side layer.

## What we guarantee

Across three independent layers, Harbor Clerk will not mutate your
mail server:

1. **Protocol layer.** Every mailbox is opened with the IMAP `EXAMINE`
   command, not `SELECT`. The IMAP server itself rejects `STORE`,
   `EXPUNGE`, `COPY`, `MOVE`, `APPEND` on an `EXAMINE`'d selection —
   no matter what bytes the client sends.

2. **Subclass layer.** Harbor Clerk's `ReadOnlyIMAP4_SSL` overrides
   every mutating method on the underlying aioimaplib client (`select`,
   `store`, `copy`, `move`, `expunge`, `append`, `create`, `delete`,
   `rename`, `subscribe`, `unsubscribe`) to raise before any bytes
   leave the process. The polymorphic `uid()` entry point inspects
   its subcommand and refuses `STORE / COPY / MOVE / EXPUNGE`.

3. **Wrapper layer.** All IMAP access goes through `IMAPConnection`,
   which exposes only the read-only operations we need. There is no
   escape hatch to the underlying client. A unit test (`tests/mail/
   test_no_mutation_static.py`) AST-scans the codebase to assert
   that no production code calls a mutating method by name.

Additionally, every IMAP command is recorded to the `imap_command_log`
table for 30 days, with credentials redacted. If something does go
wrong, there's an auditable record of every operation.

## Additional server-side safeguards

The layers above are client-side defenses. If you self-host your mail
server (Dovecot, Cyrus, etc.), you can add a fourth, server-enforced
layer by giving Harbor Clerk a read-only account.

### Dovecot

Create a dedicated read-only IMAP user with `ACL r` permissions on
the folders Harbor Clerk should index:

```
doveadm acl set INBOX user=harborclerk@example.org lookup read
```

This guarantees that even if Harbor Clerk were compromised, the
credentials it holds cannot perform writes.

### Other servers

- **Cyrus IMAPd:** `cyradm` → `setaclmailbox user.alice harborclerk lrs`
- **Courier-IMAP:** does not have per-folder ACLs; use OS-level
  permissions on the maildir.

### SaaS providers

Most SaaS IMAP providers do not offer per-user read-only credentials
over IMAP. Use the strongest available access:

- **Gmail:** Use an app password. (When OAuth support is added to
  Harbor Clerk, switch to the `https://www.googleapis.com/auth/
  gmail.readonly` scope.)
- **Microsoft 365 / Outlook.com:** Use an app password. (When OAuth
  support is added, use the `Mail.Read` Graph scope.)
- **iCloud Mail:** Use an app-specific password. iCloud has no
  per-credential scope mechanism.
- **Fastmail:** Use an app password with the "Mail" scope only — no
  separate read-only option, but Fastmail does support OAuth scopes
  for some integrations.

## Verifying read-only behavior

Run the no-mutation invariant test against your own server:

```bash
DOVECOT_TEST_HOST=your.mail.host DOVECOT_TEST_PORT=993 \
  uv run pytest tests/integration/test_mail_e2e_dovecot.py::test_no_mutation_invariant_against_dovecot -v -m integration
```

This snapshots your mailbox before and after a full Harbor Clerk
ingestion cycle and verifies that nothing changed.
```

- [ ] **Step 2: Commit**

```bash
git add docs/imap-readonly.md
git commit -m "docs: per-provider IMAP read-only safeguards

Explains the three client-side layers (protocol EXAMINE, subclass,
wrapper) and how self-hosters can add a fourth server-side layer
via ACLs. Notes per-SaaS-provider limitations (no per-credential
read-only on Gmail / Outlook / iCloud until OAuth support lands)."
```

---

## Task 16: Final pre-PR review

Per the standing directive in `MEMORY.md`: dispatch a fresh-eyes review before merging anything multi-component.

- [ ] **Step 1: Run the full test suite and linters**

Run in parallel:
```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Expected: all PASS.

- [ ] **Step 2: Dispatch a `feature-dev:code-reviewer` subagent**

Use a minimal prompt — no focus areas, no carve-outs — per the directive in `MEMORY.md`:

```
Review the branch tip vs main. Report bugs, logic errors, security
issues, code-quality problems, and convention violations at >=80
confidence.
```

- [ ] **Step 3: Address findings ≥80 confidence before opening the PR**

For each finding, fix in a new commit with a message like `fix(<area>): <what>`. Do not amend prior commits.

- [ ] **Step 4: Open the PR**

```bash
git push -u origin <branch>
gh pr create --title "feat(mail): read-only IMAP safeguards + audit log" --body "$(cat <<'EOF'
## Summary

Harden Harbor Clerk's existing IMAP mail ingestion to be provably
read-only at three layers, and add a redaction-aware audit log of
every IMAP command issued.

**Layers:**
- **Protocol (EXAMINE not SELECT):** three call sites in sync.py /
  lifecycle.py migrated from `select()` to `examine()`. Server-enforced
  read-only selection.
- **Wrapper:** `IMAPConnection` exposes only read-only operations;
  `.client` escape hatch removed. `uid()` gates STORE/COPY/MOVE/EXPUNGE.
- **Subclass:** `ReadOnlyIMAP4_SSL(aioimaplib.IMAP4_SSL)` raises on
  every mutating method as a last-resort defense.
- **Test invariants:** `FakeIMAP` enforces the same constraints in
  unit tests; static AST guard scans `mail/` for mutating calls;
  integration test against real Dovecot snapshots server state
  before and after a full ingestion cycle.

**Audit log:**
- New `imap_command_log` table (account_id, label_path, command,
  args_redacted, response_status, response_bytes, duration_ms,
  error, created_at).
- LOGIN passwords and XOAUTH2 tokens redacted before storage.
- Response bodies are never persisted — only byte counts.
- Hourly reaper drops rows older than 30 days.

## Out of scope / follow-ups

- OAuth read-only scopes (Gmail / Microsoft Graph). The codebase
  currently has no OAuth support. When added, scopes must be
  `gmail.readonly` and `Mail.Read`.
- MCP tool-call audit (sibling project). Will reuse the
  `<domain>_log` naming convention and the reaper pattern
  introduced here.
- Unified audit-log UI. IMAP audit is incident-driven and SQL-queryable
  only in phase 1.

## Test plan
- [x] Unit tests: `tests/mail/test_imap_client.py`, `test_readonly_imap.py`,
      `test_imap_audit.py`, `test_no_mutation_static.py` — all pass.
- [x] Existing mail suite still green: `tests/mail/`, `tests/api/test_mail_routes.py`.
- [ ] Integration test against local Dovecot:
      `pytest tests/integration/test_mail_e2e_dovecot.py -m integration`.
- [ ] Migrate forward + downgrade + forward to verify migration is reversible.
EOF
)"
```

- [ ] **Step 5: Record out-of-scope items in `pr_followups.md`**

Per the standing directive in `MEMORY.md`, add an entry to
`/Users/alex/.claude/projects/-Users-alex-mcp-gateway/memory/pr_followups.md`
covering:
- OAuth read-only scopes (Gmail, Microsoft) — to be paired with adding OAuth auth.
- MCP tool-call audit — sibling project; will reuse `<domain>_log` convention.
- Unified audit-log UI — defer until at least two audit consumers exist.

---

## Self-Review

**Spec coverage:**
- Layer 1 (EXAMINE): ✅ Tasks 1–2.
- Layer 2 (wrapped client + subclass): ✅ Tasks 1, 3–7.
- Layer 3 (OAuth scopes): ⚠️ explicitly out of scope; recorded in PR description and `pr_followups.md` (Task 16 Step 5).
- Layer 4 (per-provider docs): ✅ Task 15.
- Layer 5 (CI integration test): ✅ Task 14.
- Audit log infrastructure: ✅ Tasks 8–12.
- No `mark-as-read after ingest` ever: ✅ enforced by the subclass (Task 5) + static guard (Task 13) + Dovecot snapshot test (Task 14).
- Naming / schema conventions for future MCP audit reuse: ✅ `<domain>_log` table name; `account_id`/`created_at` indexed; `details`-style JSONB not used here because IMAP args are simple text (intentional — MCP audit can introduce JSONB when its richer args need it).
- 30-day reaper: ✅ Task 10 implementation + Task 12 scheduling.
- Redaction discipline (LOGIN, XOAUTH2, response bodies): ✅ Task 10.

**Placeholder scan:**
- "Apply the same pattern to every public method" in Task 11 Step 3 — borderline; the pattern is fully shown for `examine()` and the list of methods is exact, so an engineer can apply it mechanically. Acceptable.
- "The exact pattern depends on what the file looks like" in Task 12 Step 4 — flagged. Mitigated by Step 1 (read the existing pattern) and a concrete code template. Acceptable.

**Type consistency:**
- `ImapCommandLog` columns used in Tasks 9, 10, 11, 12 all reference the same names defined in Task 9.
- `IMAPConnection` methods (`examine`, `fetch`, `uid`, `uid_search`, `list_mailboxes`, `capability`, `idle_start`, `idle_done`, `wait_server_push`) consistent across Tasks 3, 4, 11.
- `ReadOnlyIMAP4_SSL` mutating-method names consistent across Tasks 5, 7.
- `redact_imap_args(command, args)` signature consistent across Task 10 and Task 11.

**Order-of-operations:**
- Wrapper changes (Tasks 1–4) precede subclass wiring (Task 6) so test patches can target the new class name.
- Subclass wiring (Task 6) precedes FakeIMAP tightening (Task 7) so the patches in Task 6 Step 4 land before the fake's invariants become strict.
- Audit log schema (Task 8) → model (Task 9) → helper (Task 10) → wiring (Task 11) → scheduling (Task 12): linear, no cycles.
- Static guard (Task 13) and integration test (Task 14) come after all production hardening so they assert the final state.
