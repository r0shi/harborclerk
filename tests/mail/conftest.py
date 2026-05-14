"""Shared fixtures for mail tests.

The FakeIMAP class is a minimal in-process replacement for
aioimaplib.IMAP4_SSL — it implements just the methods the sync engine
calls and lets tests stage canned responses.
"""

from __future__ import annotations


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

    async def examine(self, mailbox: str):
        resp = self._select_response or _Response("OK", [])
        return resp.result, resp.lines

    async def select(self, mailbox: str):
        resp = self._select_response or _Response("OK", [])
        return resp.result, resp.lines

    async def uid_search(self, *criteria: str, charset: str | None = None):
        # Task 7 TODO: tighten FakeIMAP uid_search to assert read-only criteria shapes.
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

    def has_capability(self, name: str) -> bool:
        """Task 7 TODO: replace with a proper capability-set stub.

        Parses the staged _capability_response lines to answer the query,
        matching the real aioimaplib behaviour of checking capabilities
        populated at connect time. This passthrough is the minimal-disruption
        fix to keep tests green after the conn.client.capability() latent-bug
        removal in Task 4.
        """
        resp = self._capability_response or _Response("OK", [b"CAPABILITY IMAP4rev1 IDLE"])
        blob = b" ".join(resp.lines).upper()
        return name.upper().encode() in blob.split()

    async def idle_start(self, timeout: float = 0):
        return "OK", []

    def idle_done(self):
        return "OK", []

    async def wait_server_push(self, timeout: float | None = None) -> list[bytes]:
        # Match real aioimaplib: IdleCommand.queue holds list[bytes] (each
        # element is one IMAP response line); wait_server_push() returns one
        # such list per push. Tests stage individual lines via set_idle_events;
        # we wrap each as [line] to mirror the real shape.
        if self._idle_events:
            return [self._idle_events.pop(0)]
        raise TimeoutError("no idle events queued")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_fake_imap_state():
    """Reset FakeIMAP's class-level response staging between every mail test.

    Without this, leftover responses from a previous test leak into the
    next — rendering tests order-dependent.
    """
    FakeIMAP.reset()
    yield
    FakeIMAP.reset()


from uuid import uuid4  # noqa: E402

from harbor_clerk.models import MailAccount, WatchedLabel  # noqa: E402


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
