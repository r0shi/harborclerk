"""Tests for the email-ingest pipeline (watched_messages → Documents)."""

import pytest

from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.ingest import fetch_eml_bytes


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_fetch_eml_bytes_returns_raw_message(mock_aioimap, watched_label):
    eml = b"From: a@example.com\r\nSubject: t\r\n\r\nBody"
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_uid_fetch_response(
        "OK",
        [
            b"1 (UID 1 BODY[] {%d}" % len(eml),
            eml,
            b")",
            b"OK FETCH completed",
        ],
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    fetched = await fetch_eml_bytes(conn, uid=1)
    await conn.logout()

    assert fetched == eml
