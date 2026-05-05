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
    fetched = (await db_session.execute(select(MailAccount).where(MailAccount.account_id == account_id))).scalar_one()
    assert fetched.app_password_ciphertext != b"abcd-efgh-ijkl-mnop"
    assert len(fetched.key_fingerprint) == 8


async def test_create_mail_account_requires_admin(client, regular_user, user_token):
    body = {
        "display_name": "x",
        "provider": "gmail",
        "imap_host": "h",
        "imap_port": 993,
        "imap_username": "u",
        "app_password": "p",
    }
    resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(user_token))
    assert resp.status_code == 403


async def test_create_mail_account_duplicate_returns_409(client, admin_user, admin_token):
    body = {
        "display_name": "first",
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "dup@example.com",
        "app_password": "x",
    }
    r1 = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    assert r1.status_code == 201
    body["display_name"] = "second"
    r2 = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    assert r2.status_code == 409


async def test_list_mail_accounts(client, admin_user, admin_token, db_session):
    body = {
        "display_name": "list-test",
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "list@example.com",
        "app_password": "x",
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
        "display_name": "del-test",
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "del@example.com",
        "app_password": "x",
    }
    create_resp = await client.post("/api/mail/accounts", json=body, headers=auth_header(admin_token))
    account_id = create_resp.json()["account_id"]

    del_resp = await client.delete(f"/api/mail/accounts/{account_id}", headers=auth_header(admin_token))
    assert del_resp.status_code == 204

    # Verify gone
    list_resp = await client.get("/api/mail/accounts", headers=auth_header(admin_token))
    usernames = {a["imap_username"] for a in list_resp.json()}
    assert "del@example.com" not in usernames


async def test_test_connection_endpoint(client, admin_user, admin_token, monkeypatch):
    """Test connection endpoint: opens IMAP conn, runs LIST, returns folders."""
    from tests.mail.conftest import FakeIMAP

    FakeIMAP.reset()
    FakeIMAP.set_login_response("OK", b"OK")
    FakeIMAP.set_list_response(
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Clerk"',
            b"OK LIST completed",
        ],
    )
    monkeypatch.setattr("harbor_clerk.mail.imap_client.aioimaplib.IMAP4_SSL", FakeIMAP)

    body = {
        "display_name": "test-conn",
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "tc@example.com",
        "app_password": "good",
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
        "display_name": "auth-fail",
        "provider": "gmail",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "imap_username": "fail@example.com",
        "app_password": "wrong",
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
