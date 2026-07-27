"""IMAPConnection wraps aioimaplib with typed exceptions."""

import pytest

from harbor_clerk.mail.exceptions import AuthError, ReadOnlyViolation
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


@pytest.fixture
def mock_aioimap(monkeypatch):
    """Patch aioimaplib.IMAP4_SSL with an in-process fake.

    Returns the FakeIMAP class so individual tests can set its `result`
    attribute to control LOGIN behavior.
    """
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
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


@pytest.fixture
def _patch_aioimaplib(monkeypatch):
    """Patch aioimaplib.IMAP4_SSL with FakeIMAP (same as mock_aioimap, named
    for tests that do their own per-method monkeypatching on FakeIMAP)."""
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)


async def test_examine_uses_examine_not_select(_patch_aioimaplib, monkeypatch):
    """examine() must call the underlying client's examine(), never select().

    Rationale: select() opens the mailbox read-write; examine() opens it
    read-only and the IMAP server itself rejects mutations on the selection.
    """
    from harbor_clerk.mail.imap_client import IMAPConnection
    from tests.mail.conftest import FakeIMAP

    calls: list[str] = []

    async def _fake_examine(self, mailbox):
        calls.append(f"examine:{mailbox}")
        return "OK", [b"OK [READ-ONLY]"]

    async def _fake_select(self, mailbox):  # should never be called
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


@pytest.mark.parametrize("verb", ["STORE", "store", "COPY", "MOVE", "EXPUNGE"])
async def test_uid_blocks_mutating_subcommands(_patch_aioimaplib, verb):
    from harbor_clerk.mail.imap_client import IMAPConnection

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    with pytest.raises(ReadOnlyViolation, match=verb.upper()):
        await conn.uid(verb, "1:*", r"+FLAGS (\Seen)")


async def test_has_capability_delegates_to_underlying_client(_patch_aioimaplib, monkeypatch):
    """has_capability() should query the underlying client's has_capability."""
    from harbor_clerk.mail.imap_client import IMAPConnection
    from tests.mail.conftest import FakeIMAP

    calls: list[str] = []

    def _fake_has_capability(self, name: str) -> bool:
        calls.append(name)
        return name == "IDLE"

    monkeypatch.setattr(FakeIMAP, "has_capability", _fake_has_capability, raising=False)
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()

    assert conn.has_capability("IDLE") is True
    assert conn.has_capability("STARTTLS") is False
    assert calls == ["IDLE", "STARTTLS"]


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


async def test_logout_closes_the_transport_even_when_login_never_succeeded():
    """Dropping the client reference does not release the socket.

    aioimaplib schedules `loop.create_connection(...)` in `IMAP4.__init__`, so
    the transport is owned by the event loop; losing the Python reference leaves
    it open until the server reaps it (~30 min on Gmail). LOGOUT is skipped when
    `_logged_in` is false — the connect-ok/login-failed path — so before this
    fix that path closed nothing at all.

    Harmless when a dead task was never retried: one socket per watcher
    lifetime. With respawn-on-failure it repeats, against Gmail's limit of 15
    simultaneous IMAP connections per account.
    """
    import asyncio

    from harbor_clerk.mail.imap_client import IMAPConnection

    closed = []

    class _Transport:
        def close(self):
            closed.append(True)

    logouts = []

    class _Client:
        def __init__(self):
            self.protocol = type("P", (), {"transport": _Transport()})()
            self._client_task = asyncio.get_event_loop().create_future()
            self._client_task.set_result(None)

        async def logout(self):
            # Recorded, not raised: `logout()` wraps this call in
            # `except Exception`, which swallows AssertionError — so raising
            # here would make the guard inert.
            logouts.append(1)

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    conn._client = _Client()
    conn._logged_in = False  # connected, but login raised

    await conn.logout()

    assert closed, "transport was never closed — the socket leaks until the server reaps it"
    assert logouts == [], "sent LOGOUT when login never succeeded"
    assert conn._client is None


async def test_logout_cancels_a_still_pending_connection_task():
    """`create_connection` runs as a task nobody awaits.

    The earlier version of this test used an already-failed task, so
    `task.done()` held unconditionally and `task.exception()` — the assertion
    itself — was what retrieved the exception. It passed against the unfixed
    code. This uses a *pending* task, which only reaches a terminal state if
    `logout()` actually cancels it.
    """
    import asyncio

    from harbor_clerk.mail.imap_client import IMAPConnection

    async def _never_connects():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_never_connects())
    await asyncio.sleep(0)
    assert not task.done()

    class _Client:
        protocol = None

        def __init__(self, t):
            self._client_task = t

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    conn._client = _Client(task)
    conn._logged_in = False

    await conn.logout()

    assert task.done(), "pending connection task was left running"
    assert task.cancelled()


def test_aioimaplib_still_exposes_the_attributes_logout_reaches_for():
    """`logout()` reaches past the wrapper for the socket, so it depends on two
    aioimaplib internals. The other tests here use stubs with those names
    hard-coded, which would keep passing through an upstream rename while the
    leak silently returned. Pin them against the real library.
    """
    import aioimaplib

    assert "_client_task" in aioimaplib.IMAP4.__annotations__ or hasattr(aioimaplib.IMAP4, "_client_task"), (
        "aioimaplib.IMAP4._client_task is gone — logout() no longer drains the connection task"
    )

    protocol = aioimaplib.IMAP4ClientProtocol.__new__(aioimaplib.IMAP4ClientProtocol)
    assert hasattr(protocol, "transport") or "transport" in aioimaplib.IMAP4ClientProtocol.__init__.__code__.co_names, (
        "IMAP4ClientProtocol.transport is gone — logout() no longer closes the socket"
    )

    # The third internal, and the one whose loss is most silent: without
    # `protocol`, both getattrs return None and the close no-ops while every
    # stub-based test stays green.
    created = aioimaplib.IMAP4.create_client.__code__.co_names
    assert "protocol" in created, "IMAP4.protocol is gone — logout() can no longer reach the transport"
    assert "_client_task" in created, "IMAP4._client_task is no longer assigned — logout() drains nothing"
