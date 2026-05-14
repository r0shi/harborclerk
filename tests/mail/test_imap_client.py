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


@pytest.fixture
def _patch_aioimaplib(monkeypatch):
    """Patch aioimaplib.IMAP4_SSL with FakeIMAP (same as mock_aioimap, named
    for tests that do their own per-method monkeypatching on FakeIMAP)."""
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)


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
