"""Label/folder discovery: IMAP LIST → Folder tree, with system-folder
detection."""

import pytest

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.labels import Folder, discover_folders


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_discover_folders_parses_list_response(mock_aioimap):
    # Standard Gmail-style LIST output: each line is `* LIST (flags) "delim" "name"`.
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_list_response(
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Clerk"',
            b'(\\HasChildren) "/" "Clerk/Contracts"',
            b'(\\HasNoChildren \\Noselect) "/" "[Gmail]"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
            b"OK LIST completed",
        ],
    )
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
    assert inbox.is_system is True  # corrected: INBOX is a system folder

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
