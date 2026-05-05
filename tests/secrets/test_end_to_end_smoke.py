"""End-to-end smoke: cipher → mail_accounts row → DB → fetch → decrypt."""

import base64
import os

import pytest
from sqlalchemy import select

from harbor_clerk.models import MailAccount
from harbor_clerk.secrets import get_cipher


@pytest.fixture(autouse=True)
def reset_cipher_singleton():
    from harbor_clerk.secrets import _accessor

    _accessor.reset()
    yield
    _accessor.reset()


async def test_round_trip_through_database(db_session, monkeypatch):
    monkeypatch.setenv(
        "HARBOR_CLERK_MASTER_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )

    cipher = get_cipher()
    plaintext_password = b"abcd-efgh-ijkl-mnop"
    ciphertext, fingerprint = cipher.encrypt(plaintext_password)

    account = MailAccount(
        display_name="Round-trip test",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="roundtrip@example.com",
        app_password_ciphertext=ciphertext,
        key_fingerprint=fingerprint,
    )
    db_session.add(account)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(MailAccount).where(MailAccount.imap_username == "roundtrip@example.com"))
    ).scalar_one()

    decrypted = cipher.decrypt(fetched.app_password_ciphertext, fetched.key_fingerprint)
    assert decrypted == plaintext_password
