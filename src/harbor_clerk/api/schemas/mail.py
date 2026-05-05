"""Pydantic schemas for /api/mail/* endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, SecretStr

ProviderName = Literal["gmail", "icloud", "fastmail", "yahoo", "generic"]
AccountStatus = Literal["active", "auth_error", "key_mismatch", "paused"]
LabelStatus = Literal["active", "paused", "error"]


class MailAccountCreate(BaseModel):
    """Body for `POST /api/mail/accounts`. The app password is encrypted
    with the process-wide Cipher before being stored; the plaintext lives
    only in this request object and the in-memory IMAPConnection used for
    the test-connection probe."""

    display_name: str = Field(min_length=1, max_length=200)
    provider: ProviderName
    imap_host: str = Field(min_length=1, max_length=255)
    imap_port: int = Field(ge=1, le=65535)
    imap_username: str = Field(min_length=1, max_length=255)
    app_password: SecretStr = Field(min_length=1, max_length=500)


class MailAccountResponse(BaseModel):
    """Response shape for `GET /api/mail/accounts` and individual fetches.
    Never includes the password — neither plaintext nor ciphertext."""

    account_id: UUID
    display_name: str
    provider: ProviderName
    imap_host: str
    imap_port: int
    imap_username: str
    status: AccountStatus
    last_error: str | None
    last_connected_at: datetime | None
    created_at: datetime


class FolderInfo(BaseModel):
    """One folder/label entry as discovered via LIST."""

    path: str
    display_name: str
    is_system: bool
    has_children: bool


class TestConnectionResponse(BaseModel):
    """Response from `POST /api/mail/accounts/{id}/test`. Indicates whether
    the credentials work and, if so, returns the discovered folder list
    so the wizard can render a label picker without a second API call."""

    success: bool
    error: str | None = None
    folders: list[FolderInfo] = Field(default_factory=list)


class WatchedLabelCreate(BaseModel):
    """Body for `POST /api/mail/labels`."""

    account_id: UUID
    label_path: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=200)


class WatchedLabelResponse(BaseModel):
    label_id: UUID
    account_id: UUID
    label_path: str
    display_name: str
    status: LabelStatus
    last_error: str | None
    last_synced_at: datetime | None
    last_uid_seen: int
    uidvalidity: int | None
    created_at: datetime
