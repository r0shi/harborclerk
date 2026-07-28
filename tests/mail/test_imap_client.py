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


class _RecordingTransport:
    def __init__(self, closed: list):
        self._closed = closed

    def close(self):
        self._closed.append(1)


def _connected_client(closed: list, logouts: list, logout_raises: BaseException | None = None):
    """Shaped like a real post-handshake aioimaplib client: a protocol holding
    the transport, and a *completed* connection task — which is what a
    successful login implies."""
    import asyncio

    class _Client:
        def __init__(self):
            self.protocol = type("P", (), {"transport": _RecordingTransport(closed)})()
            self._client_task = asyncio.get_event_loop().create_future()
            self._client_task.set_result(None)

        async def logout(self):
            logouts.append(1)
            if logout_raises is not None:
                raise logout_raises
            return "OK", [b"BYE"]

    return _Client()


async def test_logout_closes_the_transport_after_a_successful_login():
    """The production-common path: every watcher shutdown and every label
    cancel goes through logged_in=True → LOGOUT → close. Every other socket
    test here drives the login-failed path, so a regression that closes the
    socket only when login failed would ship green without this one.
    """
    from harbor_clerk.mail.imap_client import IMAPConnection

    closed: list = []
    logouts: list = []
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    conn._client = _connected_client(closed, logouts)
    conn._logged_in = True

    await conn.logout()

    assert logouts == [1], "LOGOUT was never sent on a logged-in connection"
    assert closed, "transport was never closed on the logged-in path"
    assert conn._client is None
    assert conn._logged_in is False


async def test_logout_closes_the_transport_even_when_the_logout_command_fails():
    """A server that errors or drops the connection mid-LOGOUT must not leak
    the socket — the LOGOUT is best-effort, the close is not."""
    from harbor_clerk.mail.imap_client import IMAPConnection

    closed: list = []
    logouts: list = []
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    conn._client = _connected_client(closed, logouts, logout_raises=RuntimeError("connection reset"))
    conn._logged_in = True

    await conn.logout()  # must not raise: LOGOUT errors are swallowed

    assert logouts == [1]
    assert closed, "transport leaked because the LOGOUT command failed"


async def test_logout_closes_the_transport_when_cancelled_mid_logout():
    """CancelledError is not an Exception: without the finally, a cancellation
    delivered during the LOGOUT — the normal way a watcher label task exits —
    would skip the close and the drain, on exactly the path the docstring
    promises cannot leak. The cancellation itself must still propagate.
    """
    import asyncio

    from harbor_clerk.mail.imap_client import IMAPConnection

    closed: list = []
    logouts: list = []
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    client = _connected_client(closed, logouts, logout_raises=asyncio.CancelledError())
    # A pending connection task cannot coexist with logged_in=True against the
    # real library — a completed login implies it finished. Staged here anyway,
    # because it is the only way to observe that the drain half of the cleanup
    # runs on the cancellation path and not just the fall-through one.
    pending = asyncio.get_event_loop().create_future()
    client._client_task = pending

    conn._client = client
    conn._logged_in = True

    with pytest.raises(asyncio.CancelledError):
        await conn.logout()

    assert closed, "cancellation mid-LOGOUT leaked the socket"
    assert pending.cancelled(), "the connection-task drain was skipped on the cancellation path"
    assert conn._client is None


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

    # Constructed for real, not via __new__: on a bare __new__ instance the
    # attribute does not exist yet (it is assigned in __init__), so a hasattr
    # check there is always False and the pin degrades to a string search that
    # any textual survival of the word "transport" keeps green.
    protocol = aioimaplib.IMAP4ClientProtocol(None)
    assert hasattr(protocol, "transport"), (
        "IMAP4ClientProtocol.transport is gone — logout() no longer closes the socket"
    )

    # The third internal, and the one whose loss is most silent: without
    # `protocol`, both getattrs return None and the close no-ops while every
    # stub-based test stays green.
    created = aioimaplib.IMAP4.create_client.__code__.co_names
    assert "protocol" in created, "IMAP4.protocol is gone — logout() can no longer reach the transport"
    assert "_client_task" in created, "IMAP4._client_task is no longer assigned — logout() drains nothing"
