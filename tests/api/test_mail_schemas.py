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
