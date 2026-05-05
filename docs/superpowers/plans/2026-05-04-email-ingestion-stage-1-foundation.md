# Email Ingestion — Stage 1: Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the schema, secrets-encryption primitive, and macOS Keychain bootstrap that all subsequent email-ingestion stages depend on. Nothing user-visible after this stage; all changes are foundation.

**Architecture:** Three new tables (`mail_accounts`, `watched_labels`, `watched_messages`) plus nullable email-metadata columns on `documents`. A new `harbor_clerk.secrets` module provides Fernet envelope encryption with key fingerprinting; the master key is sourced from the `HARBOR_CLERK_MASTER_KEY` env var on every platform (Docker operator sets it directly; macOS Swift reads from Keychain and exports it to Python subprocess environments). One env-var code path; Keychain is a macOS implementation detail.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, Alembic, PostgreSQL 18, `cryptography` (Fernet — already a transitive dep via authlib, made explicit here), Swift 5.9 (`Security` framework for Keychain), `XCTest`.

**Spec:** [`docs/superpowers/specs/2026-05-04-email-ingestion-design.md`](../specs/2026-05-04-email-ingestion-design.md)

**Implementation deviation from spec:** Spec section "Master key sourcing" said macOS would pass the key via `NATIVE_CONFIG_FILE` (config.json). This plan instead uses the `HARBOR_CLERK_MASTER_KEY` env var on macOS *and* Docker. Reason: putting the raw key in a persistent JSON file at a stable filesystem path is readable by any other process running as the same user and is included in Time Machine backups, defeating Keychain's protection. Env vars exist only in process memory and inherit naturally to child processes. Same env var name unifies both deployment paths into one Python code path. Spec stays authoritative on the user-visible behavior; this is an implementation-detail refinement.

---

## File Structure

**New files:**
- `alembic/versions/0020_email_ingest_foundation.py` — schema migration
- `src/harbor_clerk/models/mail_account.py` — `MailAccount` SQLAlchemy model
- `src/harbor_clerk/models/watched_label.py` — `WatchedLabel` model
- `src/harbor_clerk/models/watched_message.py` — `WatchedMessage` model
- `src/harbor_clerk/secrets/__init__.py`
- `src/harbor_clerk/secrets/cipher.py` — Fernet wrapper + fingerprinting + `KeyMismatch` exception
- `src/harbor_clerk/secrets/keysource.py` — `get_master_key()` + `MissingMasterKey` exception
- `tests/secrets/__init__.py`
- `tests/secrets/test_cipher.py`
- `tests/secrets/test_keysource.py`
- `tests/models/__init__.py` — only if `tests/models/` doesn't already exist; otherwise skip
- `tests/models/test_mail_models.py`
- `tests/test_alembic_0020_email_ingest.py` — schema smoke test
- `macos/HarborClerkServer/HarborClerkServer/MasterKeyManager.swift` — Keychain CRUD for the master key
- `macos/HarborClerkServer/HarborClerkServerTests/MasterKeyManagerTests.swift`
- `docs/secrets-and-keys.md` — operator docs for master-key lifecycle

**Modified files:**
- `pyproject.toml:7-32` — add explicit `cryptography>=44.0.0` dep
- `src/harbor_clerk/models/__init__.py` — register new models
- `src/harbor_clerk/models/document.py` — add nullable email columns
- `src/harbor_clerk/config.py:50-55` — add `master_key_b64` field with `HARBOR_CLERK_MASTER_KEY` validation alias
- `macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift` — first-launch master-key bootstrap
- `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift:880-895` — inject `HARBOR_CLERK_MASTER_KEY` into subprocess env
- `docker-compose.yml` — document `HARBOR_CLERK_MASTER_KEY` env var on `app`, `worker-io`, `worker-cpu`, `watcher` services
- `.env.example` — document the env var with a generation snippet
- `docs/architecture.md` — link to new `secrets-and-keys.md`

---

## Task 1: Schema migration

**Files:**
- Create: `alembic/versions/0020_email_ingest_foundation.py`
- Test: `tests/test_alembic_0020_email_ingest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alembic_0020_email_ingest.py
"""Smoke test for migration 0020 — schema shape only.

The full SQLAlchemy model coverage lives in tests/models/test_mail_models.py.
This file just verifies the migration ran and produced the expected DDL.

Pattern note: depends on the session-scoped `_engine` fixture from
tests/conftest.py — that fixture is the one that runs `alembic upgrade head`,
so depending on it guarantees migrations have been applied. We then open a
sync connection for SQLAlchemy's `inspect()` (the inspector wants a sync
bind; the project's main DB usage is async).
"""

import os

import pytest
from sqlalchemy import create_engine, inspect


@pytest.fixture
def sync_engine(_engine):
    """Sync engine for schema introspection. Depends on `_engine` to ensure
    migrations have been applied at session scope."""
    sync_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_mail_accounts_table_shape(sync_engine):
    insp = inspect(sync_engine)
    assert "mail_accounts" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("mail_accounts")}
    for required in (
        "account_id",
        "display_name",
        "provider",
        "imap_host",
        "imap_port",
        "imap_username",
        "app_password_ciphertext",
        "key_fingerprint",
        "status",
        "last_error",
        "last_connected_at",
        "created_at",
    ):
        assert required in cols, f"missing column mail_accounts.{required}"
    unique_cols = [tuple(c["column_names"]) for c in insp.get_unique_constraints("mail_accounts")]
    assert ("imap_host", "imap_username") in unique_cols


def test_watched_labels_table_shape(sync_engine):
    insp = inspect(sync_engine)
    assert "watched_labels" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("watched_labels")}
    for required in (
        "label_id",
        "account_id",
        "label_path",
        "display_name",
        "uidvalidity",
        "last_uid_seen",
        "status",
        "last_error",
        "last_synced_at",
        "created_at",
    ):
        assert required in cols, f"missing column watched_labels.{required}"
    unique_cols = [tuple(c["column_names"]) for c in insp.get_unique_constraints("watched_labels")]
    assert ("account_id", "label_path") in unique_cols


def test_watched_messages_table_shape(sync_engine):
    insp = inspect(sync_engine)
    assert "watched_messages" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("watched_messages")}
    for required in (
        "message_pk",
        "label_id",
        "message_id",
        "imap_uid",
        "eml_sha256",
        "email_doc_id",
        "status",
        "first_seen_at",
        "unlabeled_at",
    ):
        assert required in cols, f"missing column watched_messages.{required}"
    unique_cols = [tuple(c["column_names"]) for c in insp.get_unique_constraints("watched_messages")]
    assert ("label_id", "message_id") in unique_cols
    index_names = {ix["name"] for ix in insp.get_indexes("watched_messages")}
    assert "watched_messages_label_status" in index_names
    assert "watched_messages_eml_sha" in index_names


def test_documents_email_columns_added(sync_engine):
    insp = inspect(sync_engine)
    cols = {c["name"]: c for c in insp.get_columns("documents")}
    for required in (
        "email_message_id",
        "email_thread_id",
        "email_parent_doc_id",
        "email_from_address",
        "email_from_name",
        "email_to_addresses",
        "email_cc_addresses",
        "email_date_sent",
        "email_label_path",
    ):
        assert required in cols, f"missing column documents.{required}"
    index_names = {ix["name"] for ix in insp.get_indexes("documents")}
    assert "documents_email_message_id" in index_names
    assert "documents_email_thread_id" in index_names
    assert "documents_email_parent" in index_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_alembic_0020_email_ingest.py -v`
Expected: FAIL — `mail_accounts` table missing.

- [ ] **Step 3: Write the migration**

```python
# alembic/versions/0020_email_ingest_foundation.py
"""Foundation tables for email ingestion via IMAP labels.

Adds:
  - mail_accounts: one per IMAP connection, holds encrypted app password
  - watched_labels: one per (account, label) pair, the unit users add/remove
  - watched_messages: per-message tracking, analog of watched_files
  - documents email-metadata columns: nullable, populated only for email docs

Revision ID: 0020
Revises: 0019
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # mail_accounts
    op.create_table(
        "mail_accounts",
        sa.Column("account_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("imap_host", sa.Text(), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_username", sa.Text(), nullable=False),
        sa.Column("app_password_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("imap_host", "imap_username", name="mail_accounts_host_username_unique"),
    )

    # watched_labels
    op.create_table(
        "watched_labels",
        sa.Column("label_id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label_path", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("last_uid_seen", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("account_id", "label_path", name="watched_labels_account_path_unique"),
    )

    # watched_messages
    op.create_table(
        "watched_messages",
        sa.Column("message_pk", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "label_id",
            UUID(as_uuid=True),
            sa.ForeignKey("watched_labels.label_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("imap_uid", sa.BigInteger(), nullable=False),
        sa.Column("eml_sha256", sa.LargeBinary(), nullable=False),
        sa.Column(
            "email_doc_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("unlabeled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("label_id", "message_id", name="watched_messages_label_message_unique"),
    )
    op.create_index(
        "watched_messages_label_status",
        "watched_messages",
        ["label_id", "status"],
    )
    op.create_index(
        "watched_messages_eml_sha",
        "watched_messages",
        ["eml_sha256"],
    )

    # documents email-metadata columns
    op.add_column("documents", sa.Column("email_message_id", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("email_thread_id", sa.Text(), nullable=True))
    op.add_column(
        "documents",
        sa.Column(
            "email_parent_doc_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.doc_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("documents", sa.Column("email_from_address", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("email_from_name", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("email_to_addresses", ARRAY(sa.Text()), nullable=True))
    op.add_column("documents", sa.Column("email_cc_addresses", ARRAY(sa.Text()), nullable=True))
    op.add_column("documents", sa.Column("email_date_sent", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("email_label_path", sa.Text(), nullable=True))

    op.create_index(
        "documents_email_message_id",
        "documents",
        ["email_message_id"],
        postgresql_where=sa.text("email_message_id IS NOT NULL"),
    )
    op.create_index(
        "documents_email_thread_id",
        "documents",
        ["email_thread_id"],
        postgresql_where=sa.text("email_thread_id IS NOT NULL"),
    )
    op.create_index(
        "documents_email_parent",
        "documents",
        ["email_parent_doc_id"],
        postgresql_where=sa.text("email_parent_doc_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("documents_email_parent", table_name="documents")
    op.drop_index("documents_email_thread_id", table_name="documents")
    op.drop_index("documents_email_message_id", table_name="documents")
    op.drop_column("documents", "email_label_path")
    op.drop_column("documents", "email_date_sent")
    op.drop_column("documents", "email_cc_addresses")
    op.drop_column("documents", "email_to_addresses")
    op.drop_column("documents", "email_from_name")
    op.drop_column("documents", "email_from_address")
    op.drop_column("documents", "email_parent_doc_id")
    op.drop_column("documents", "email_thread_id")
    op.drop_column("documents", "email_message_id")

    op.drop_index("watched_messages_eml_sha", table_name="watched_messages")
    op.drop_index("watched_messages_label_status", table_name="watched_messages")
    op.drop_table("watched_messages")
    op.drop_table("watched_labels")
    op.drop_table("mail_accounts")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_alembic_0020_email_ingest.py -v`
Expected: PASS — all four schema-shape tests green.

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/0020_email_ingest_foundation.py tests/test_alembic_0020_email_ingest.py
git commit -m "feat(schema): mail_accounts, watched_labels, watched_messages, document email columns"
```

---

## Task 2: SQLAlchemy models for the three new tables

**Files:**
- Create: `src/harbor_clerk/models/mail_account.py`
- Create: `src/harbor_clerk/models/watched_label.py`
- Create: `src/harbor_clerk/models/watched_message.py`
- Modify: `src/harbor_clerk/models/__init__.py`
- Test: `tests/models/test_mail_models.py` (create `tests/models/__init__.py` if it doesn't exist)

- [ ] **Step 1: Write the failing test**

```python
# tests/models/test_mail_models.py
"""Verify the new mail models map correctly and round-trip through the DB."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from harbor_clerk.models import MailAccount, WatchedLabel, WatchedMessage


async def test_mail_account_round_trip(db_session):
    account = MailAccount(
        display_name="Test Gmail",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        imap_username="alex@example.com",
        app_password_ciphertext=b"\x00" * 100,  # placeholder; cipher comes in Task 4
        key_fingerprint=b"\x00" * 8,
        status="active",
    )
    db_session.add(account)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(MailAccount).where(MailAccount.imap_username == "alex@example.com")
        )
    ).scalar_one()
    assert fetched.provider == "gmail"
    assert fetched.imap_port == 993
    assert fetched.created_at is not None


async def test_watched_label_cascade_on_account_delete(db_session):
    account = MailAccount(
        display_name="Account to delete",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="cascade@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()

    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Clerk",
        display_name="Clerk",
    )
    db_session.add(label)
    await db_session.flush()
    label_id = label.label_id

    await db_session.delete(account)
    await db_session.flush()

    remaining = (
        await db_session.execute(
            select(WatchedLabel).where(WatchedLabel.label_id == label_id)
        )
    ).scalar_one_or_none()
    assert remaining is None, "watched_labels row should cascade-delete with the account"


async def test_watched_message_unique_per_label(db_session):
    account = MailAccount(
        display_name="Unique-test account",
        provider="generic",
        imap_host="imap.example.com",
        imap_port=993,
        imap_username="unique@example.com",
        app_password_ciphertext=b"\x00" * 100,
        key_fingerprint=b"\x00" * 8,
    )
    db_session.add(account)
    await db_session.flush()
    label = WatchedLabel(
        account_id=account.account_id,
        label_path="Clerk",
        display_name="Clerk",
    )
    db_session.add(label)
    await db_session.flush()

    msg1 = WatchedMessage(
        label_id=label.label_id,
        message_id="<abc@example.com>",
        imap_uid=1,
        eml_sha256=b"\x11" * 32,
    )
    db_session.add(msg1)
    await db_session.flush()

    msg2 = WatchedMessage(
        label_id=label.label_id,
        message_id="<abc@example.com>",  # same message_id, same label
        imap_uid=2,
        eml_sha256=b"\x22" * 32,
    )
    db_session.add(msg2)
    with pytest.raises(Exception):  # IntegrityError; exact class depends on driver
        await db_session.flush()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_mail_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'MailAccount' from 'harbor_clerk.models'`.

- [ ] **Step 3: Create `src/harbor_clerk/models/mail_account.py`**

```python
"""MailAccount model — one row per IMAP connection."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, uuid_pk


class MailAccount(Base):
    __tablename__ = "mail_accounts"
    __table_args__ = (
        UniqueConstraint("imap_host", "imap_username", name="mail_accounts_host_username_unique"),
    )

    account_id: Mapped[uuid_pk]
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # gmail|icloud|fastmail|yahoo|generic
    imap_host: Mapped[str] = mapped_column(Text, nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default="993")
    imap_username: Mapped[str] = mapped_column(Text, nullable=False)
    app_password_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_fingerprint: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]

    labels: Mapped[list["WatchedLabel"]] = relationship(  # type: ignore[name-defined]
        back_populates="account",
        cascade="all, delete-orphan",
    )
```

- [ ] **Step 4: Create `src/harbor_clerk/models/watched_label.py`**

```python
"""WatchedLabel model — one row per (mail_account, IMAP label) pair."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, uuid_pk


class WatchedLabel(Base):
    __tablename__ = "watched_labels"
    __table_args__ = (
        UniqueConstraint("account_id", "label_path", name="watched_labels_account_path_unique"),
    )

    label_id: Mapped[uuid_pk]
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mail_accounts.account_id", ondelete="CASCADE"),
        nullable=False,
    )
    label_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_uid_seen: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[created_at]

    account: Mapped["MailAccount"] = relationship(back_populates="labels")  # type: ignore[name-defined]
    messages: Mapped[list["WatchedMessage"]] = relationship(  # type: ignore[name-defined]
        back_populates="label",
        cascade="all, delete-orphan",
    )
```

- [ ] **Step 5: Create `src/harbor_clerk/models/watched_message.py`**

```python
"""WatchedMessage model — per-message tracking, analog of WatchedFile."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, LargeBinary, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from harbor_clerk.models.base import Base, created_at, uuid_pk


class WatchedMessage(Base):
    __tablename__ = "watched_messages"
    __table_args__ = (
        UniqueConstraint("label_id", "message_id", name="watched_messages_label_message_unique"),
    )

    message_pk: Mapped[uuid_pk]
    label_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("watched_labels.label_id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[str] = mapped_column(Text, nullable=False)
    imap_uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    eml_sha256: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    email_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    # Reuses the `created_at` TypeAlias from base.py — same DateTime+now()+nullable=False
    # shape, just under a different column name. The column name comes from the attribute.
    first_seen_at: Mapped[created_at]
    unlabeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    label: Mapped["WatchedLabel"] = relationship(back_populates="messages")  # type: ignore[name-defined]
```

- [ ] **Step 6: Register in `src/harbor_clerk/models/__init__.py`**

Read the file first; existing exports follow a clear pattern. Add three new entries:

```python
# Add these imports alongside the existing ones
from harbor_clerk.models.mail_account import MailAccount
from harbor_clerk.models.watched_label import WatchedLabel
from harbor_clerk.models.watched_message import WatchedMessage

# Add to __all__ if the module exports it
```

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run pytest tests/models/test_mail_models.py -v`
Expected: PASS — three tests green.

- [ ] **Step 8: Commit**

```bash
git add src/harbor_clerk/models/mail_account.py src/harbor_clerk/models/watched_label.py \
        src/harbor_clerk/models/watched_message.py src/harbor_clerk/models/__init__.py \
        tests/models/test_mail_models.py
# Also add tests/models/__init__.py if you created it
git commit -m "feat(models): MailAccount, WatchedLabel, WatchedMessage SQLAlchemy models"
```

---

## Task 3: Document model email-metadata columns

**Files:**
- Modify: `src/harbor_clerk/models/document.py`
- Test: `tests/models/test_document_email_columns.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/models/test_document_email_columns.py
"""Verify the email-metadata columns on Document map correctly."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from harbor_clerk.models import Document


from harbor_clerk.models.enums import PipelineStatus

# Minimal required fields for a Document row (sha256 + pipeline_status are NOT NULL).
# Mirrors _DOC_DEFAULTS in tests/test_api_documents.py.
_DOC_DEFAULTS = {
    "sha256": b"\x00" * 32,
    "pipeline_status": PipelineStatus.ready,
}


async def test_email_metadata_round_trip(db_session):
    doc = Document(
        title="Test email",
        canonical_filename="test.eml",
        mime_type="message/rfc822",
        email_message_id="<abc@example.com>",
        email_thread_id="thread-1",
        email_from_address="alice@example.com",
        email_from_name="Alice",
        email_to_addresses=["alex@example.com", "bob@example.com"],
        email_cc_addresses=["legal@example.com"],
        email_date_sent=datetime(2026, 4, 30, 14, 23, tzinfo=UTC),
        email_label_path="Clerk",
        **_DOC_DEFAULTS,
    )
    db_session.add(doc)
    await db_session.flush()

    fetched = (
        await db_session.execute(
            select(Document).where(Document.email_message_id == "<abc@example.com>")
        )
    ).scalar_one()
    assert fetched.email_from_address == "alice@example.com"
    assert fetched.email_to_addresses == ["alex@example.com", "bob@example.com"]
    assert fetched.email_cc_addresses == ["legal@example.com"]
    assert fetched.email_label_path == "Clerk"


async def test_email_columns_optional_for_non_email_docs(db_session):
    """Watched-folder and uploaded docs should not need to set email_* fields."""
    doc = Document(
        title="A regular PDF",
        canonical_filename="report.pdf",
        mime_type="application/pdf",
        **_DOC_DEFAULTS,
    )
    db_session.add(doc)
    await db_session.flush()

    fetched = (
        await db_session.execute(select(Document).where(Document.canonical_filename == "report.pdf"))
    ).scalar_one()
    assert fetched.email_message_id is None
    assert fetched.email_to_addresses is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/models/test_document_email_columns.py -v`
Expected: FAIL — `AttributeError: 'Document' object has no attribute 'email_message_id'`.

- [ ] **Step 3: Modify `src/harbor_clerk/models/document.py`**

Read the file first (it's the existing flat Document model from migration 0017). Add these columns alongside the existing ones — locate the section near the bottom of the column list (just before any `relationship()` calls) and append:

```python
# At the top of the file, ensure ARRAY is imported:
from sqlalchemy import ARRAY  # if not already imported

# ...inside the Document class, after the existing columns:
    # Email-ingest metadata. Populated only for email and attachment Documents;
    # NULL for watched-folder and uploaded Documents. See spec
    # docs/superpowers/specs/2026-05-04-email-ingestion-design.md.
    email_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_parent_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.doc_id", ondelete="SET NULL"),
        nullable=True,
    )
    email_from_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_from_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_to_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    email_cc_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    email_date_sent: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    email_label_path: Mapped[str | None] = mapped_column(Text, nullable=True)
```

If `uuid`, `ForeignKey`, or `UUID` aren't already imported, add them. The existing file should have most of these already.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/models/test_document_email_columns.py -v`
Expected: PASS — both tests green.

- [ ] **Step 5: Run the full Documents test suite to catch regressions**

Run: `uv run pytest tests/test_api_documents.py tests/models/ -v`
Expected: PASS — no regressions in existing Document tests.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/models/document.py tests/models/test_document_email_columns.py
git commit -m "feat(models): add email-metadata columns to Document"
```

---

## Task 4: Cipher helper — encrypt/decrypt round-trip

**Files:**
- Create: `src/harbor_clerk/secrets/__init__.py`
- Create: `src/harbor_clerk/secrets/cipher.py`
- Create: `tests/secrets/__init__.py`
- Create: `tests/secrets/test_cipher.py`
- Modify: `pyproject.toml` (explicit `cryptography` dep)

- [ ] **Step 1: Add `cryptography` to `pyproject.toml`**

In the `dependencies` list (around line 7-32), add an explicit entry. `cryptography` already comes in transitively via `authlib`, but pinning it directly makes the dep visible to `pip-audit` and avoids surprises if `authlib` ever drops it.

```toml
# Before:
dependencies = [
    "fastapi>=0.115.0",
    ...
    "watchdog>=4.0.0",
]

# After: insert "cryptography>=44.0.0" alphabetically (after "bcrypt" or "authlib"):
    "cryptography>=44.0.0",
```

Run `uv sync` to update the lock file:

```bash
uv sync
```

- [ ] **Step 2: Write the failing test**

```python
# tests/secrets/__init__.py — empty file
```

```python
# tests/secrets/test_cipher.py
"""Cipher round-trip tests."""

import os

import pytest

from harbor_clerk.secrets.cipher import Cipher


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def test_round_trip(key):
    cipher = Cipher(key)
    ciphertext, fingerprint = cipher.encrypt(b"super-secret-app-password")
    assert ciphertext != b"super-secret-app-password"
    assert len(fingerprint) == 8
    plaintext = cipher.decrypt(ciphertext, fingerprint)
    assert plaintext == b"super-secret-app-password"


def test_unicode_round_trip(key):
    cipher = Cipher(key)
    ciphertext, fingerprint = cipher.encrypt("日本語パスワード".encode())
    plaintext = cipher.decrypt(ciphertext, fingerprint)
    assert plaintext.decode() == "日本語パスワード"


def test_each_encryption_produces_different_ciphertext(key):
    """Fernet uses a random IV; identical plaintext encrypts to different ciphertext."""
    cipher = Cipher(key)
    ct1, _ = cipher.encrypt(b"hello")
    ct2, _ = cipher.encrypt(b"hello")
    assert ct1 != ct2
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/secrets/test_cipher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_clerk.secrets'`.

- [ ] **Step 4: Implement the cipher**

```python
# src/harbor_clerk/secrets/__init__.py
"""Envelope-encryption primitives for secret storage.

Postgres holds Fernet ciphertext for sensitive values (mail-account app
passwords today; potentially OAuth refresh tokens, off-host MinIO keys, etc.
later). The master key lives in the HARBOR_CLERK_MASTER_KEY env var on
every platform — operator sets it on Docker; macOS Swift reads from
Keychain and exports it on the Python subprocess environment.

Every ciphertext is paired with an 8-byte key fingerprint so cross-deployment
moves degrade gracefully (raise KeyMismatch instead of producing garbage).
"""

from harbor_clerk.secrets.cipher import Cipher, KeyMismatch

__all__ = ["Cipher", "KeyMismatch"]
```

```python
# src/harbor_clerk/secrets/cipher.py
"""Fernet wrapper with key fingerprinting.

Fernet is symmetric-AEAD (AES-128-CBC + HMAC-SHA256); the key is 32 random
bytes URL-safe-base64-encoded. We accept the raw 32 bytes as input and do
the base64 encoding inside this module so callers don't have to care about
Fernet's key format.
"""

from __future__ import annotations

import base64
import hmac
from hashlib import sha256

from cryptography.fernet import Fernet


class KeyMismatch(Exception):
    """Raised when stored fingerprint doesn't match the active master key.

    Indicates the ciphertext was encrypted under a different master key
    than the one currently loaded — typically because a deployment moved
    between hosts (e.g. Docker DB restored to macOS without exporting the
    old master key). Caller should surface this to the user as a "reconnect
    mail accounts" prompt rather than treating it as data corruption.
    """


_FINGERPRINT_DOMAIN = b"harbor-clerk-master-key-fingerprint"
_FINGERPRINT_LEN = 8


class Cipher:
    """Encrypt/decrypt with a master key and key-fingerprint check."""

    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError(f"master_key must be 32 bytes, got {len(master_key)}")
        self._master_key = master_key
        self._fernet = Fernet(base64.urlsafe_b64encode(master_key))
        self._fingerprint = _compute_fingerprint(master_key)

    @property
    def fingerprint(self) -> bytes:
        return self._fingerprint

    def encrypt(self, plaintext: bytes) -> tuple[bytes, bytes]:
        """Return (ciphertext, fingerprint) for storage."""
        return self._fernet.encrypt(plaintext), self._fingerprint

    def decrypt(self, ciphertext: bytes, stored_fingerprint: bytes) -> bytes:
        if not hmac.compare_digest(stored_fingerprint, self._fingerprint):
            raise KeyMismatch(
                "stored fingerprint does not match active master key; "
                "secret was encrypted under a different key"
            )
        return self._fernet.decrypt(ciphertext)


def _compute_fingerprint(master_key: bytes) -> bytes:
    return hmac.new(master_key, _FINGERPRINT_DOMAIN, sha256).digest()[:_FINGERPRINT_LEN]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/secrets/test_cipher.py -v`
Expected: PASS — three round-trip tests green.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/harbor_clerk/secrets/ tests/secrets/__init__.py tests/secrets/test_cipher.py
git commit -m "feat(secrets): Cipher wrapper for Fernet encrypt/decrypt round-trip"
```

---

## Task 5: Cipher helper — fingerprinting + KeyMismatch

**Files:**
- Modify: `tests/secrets/test_cipher.py` (add fingerprint tests)

- [ ] **Step 1: Add the failing tests**

Append to `tests/secrets/test_cipher.py`:

```python
def test_fingerprint_is_deterministic():
    key = b"\x42" * 32
    c1 = Cipher(key)
    c2 = Cipher(key)
    assert c1.fingerprint == c2.fingerprint
    assert len(c1.fingerprint) == 8


def test_fingerprint_differs_per_key():
    c1 = Cipher(b"\x01" * 32)
    c2 = Cipher(b"\x02" * 32)
    assert c1.fingerprint != c2.fingerprint


def test_fingerprint_does_not_leak_key():
    """Fingerprint shouldn't trivially reveal key bits."""
    key = b"\x42" * 32
    cipher = Cipher(key)
    # The fingerprint isn't derived by truncation; assert it's not a prefix or substring
    assert key[:8] != cipher.fingerprint
    assert cipher.fingerprint not in key


def test_decrypt_with_wrong_fingerprint_raises():
    from harbor_clerk.secrets.cipher import KeyMismatch

    key1 = b"\x01" * 32
    key2 = b"\x02" * 32
    cipher1 = Cipher(key1)
    cipher2 = Cipher(key2)

    ciphertext, fp1 = cipher1.encrypt(b"secret")

    # Try to decrypt with cipher2 but pass cipher1's fingerprint —
    # KeyMismatch fires before Fernet would even try.
    with pytest.raises(KeyMismatch):
        cipher2.decrypt(ciphertext, fp1)


def test_decrypt_with_matching_fingerprint_but_wrong_key_raises():
    """Defense in depth: even if fingerprint check is bypassed somehow,
    Fernet's HMAC catches a wrong-key decrypt attempt."""
    from cryptography.fernet import InvalidToken

    key1 = b"\x01" * 32
    key2 = b"\x02" * 32
    cipher1 = Cipher(key1)
    cipher2 = Cipher(key2)

    ciphertext, _ = cipher1.encrypt(b"secret")

    # Pass cipher2's fingerprint so the KeyMismatch check passes —
    # Fernet then fails on its own HMAC check.
    with pytest.raises(InvalidToken):
        cipher2.decrypt(ciphertext, cipher2.fingerprint)


def test_invalid_master_key_length_raises():
    with pytest.raises(ValueError, match="must be 32 bytes"):
        Cipher(b"too short")

    with pytest.raises(ValueError, match="must be 32 bytes"):
        Cipher(b"\x00" * 33)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/secrets/test_cipher.py -v`
Expected: PASS — all tests including the new five green. The `Cipher` implementation from Task 4 already supports these cases; this task is verifying coverage rather than adding code.

- [ ] **Step 3: Commit**

```bash
git add tests/secrets/test_cipher.py
git commit -m "test(secrets): cipher fingerprinting + KeyMismatch coverage"
```

---

## Task 6: Key source — env var resolution

**Files:**
- Create: `src/harbor_clerk/secrets/keysource.py`
- Modify: `src/harbor_clerk/secrets/__init__.py`
- Test: `tests/secrets/test_keysource.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/secrets/test_keysource.py
"""Master-key resolution from HARBOR_CLERK_MASTER_KEY env var."""

import base64
import os

import pytest

from harbor_clerk.secrets.keysource import (
    MissingMasterKey,
    get_master_key,
)


def test_env_var_returns_decoded_key(monkeypatch):
    raw = b"\x42" * 32
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", base64.b64encode(raw).decode())
    assert get_master_key() == raw


def test_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_MASTER_KEY", raising=False)
    with pytest.raises(MissingMasterKey, match="HARBOR_CLERK_MASTER_KEY"):
        get_master_key()


def test_empty_env_var_raises(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", "")
    with pytest.raises(MissingMasterKey):
        get_master_key()


def test_invalid_base64_raises(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", "not-valid-base64!!!")
    with pytest.raises(ValueError, match="not valid base64"):
        get_master_key()


def test_wrong_length_raises(monkeypatch):
    short = base64.b64encode(b"too short").decode()
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", short)
    with pytest.raises(ValueError, match="must decode to exactly 32 bytes"):
        get_master_key()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/secrets/test_keysource.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harbor_clerk.secrets.keysource'`.

- [ ] **Step 3: Implement the key source**

```python
# src/harbor_clerk/secrets/keysource.py
"""Resolve the master key from environment.

Single env var (HARBOR_CLERK_MASTER_KEY) on every platform:

- Docker: operator sets it directly in compose / env.
- macOS native: Swift Harbor Clerk Server reads from Keychain on startup
  and sets it on the environment of every Python subprocess (api, worker-io,
  worker-cpu, watcher) before launching them. See the Swift MasterKeyManager
  for the Keychain side.

The value must be base64-encoded 32-byte key material. Generation:

    python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
"""

from __future__ import annotations

import base64
import binascii
import os

_ENV_VAR = "HARBOR_CLERK_MASTER_KEY"
_KEY_LEN = 32


class MissingMasterKey(Exception):
    """Raised when the master key env var is unset or empty."""


def get_master_key() -> bytes:
    """Return the raw 32-byte master key from HARBOR_CLERK_MASTER_KEY.

    Raises MissingMasterKey if the env var is unset or empty.
    Raises ValueError if the value is not valid base64 or doesn't decode
    to exactly 32 bytes.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        raise MissingMasterKey(
            f"{_ENV_VAR} is not set. On Docker, set it in your compose env. "
            f"On macOS native, this is set automatically by the Harbor Clerk "
            f"Server menubar app — if you're seeing this error in development, "
            f"set it manually with `export {_ENV_VAR}=$(python -c 'import base64, os; "
            f"print(base64.b64encode(os.urandom(32)).decode())')`."
        )
    try:
        decoded = base64.b64decode(raw, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"{_ENV_VAR} is not valid base64: {exc}") from exc
    if len(decoded) != _KEY_LEN:
        raise ValueError(
            f"{_ENV_VAR} must decode to exactly {_KEY_LEN} bytes, got {len(decoded)}"
        )
    return decoded
```

- [ ] **Step 4: Update `src/harbor_clerk/secrets/__init__.py`**

```python
"""Envelope-encryption primitives for secret storage.

Postgres holds Fernet ciphertext for sensitive values (mail-account app
passwords today; potentially OAuth refresh tokens, off-host MinIO keys, etc.
later). The master key lives in the HARBOR_CLERK_MASTER_KEY env var on
every platform — operator sets it on Docker; macOS Swift reads from
Keychain and exports it on the Python subprocess environment.

Every ciphertext is paired with an 8-byte key fingerprint so cross-deployment
moves degrade gracefully (raise KeyMismatch instead of producing garbage).
"""

from harbor_clerk.secrets.cipher import Cipher, KeyMismatch
from harbor_clerk.secrets.keysource import MissingMasterKey, get_master_key

__all__ = ["Cipher", "KeyMismatch", "MissingMasterKey", "get_master_key"]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/secrets/test_keysource.py -v`
Expected: PASS — all five tests green.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/secrets/keysource.py src/harbor_clerk/secrets/__init__.py tests/secrets/test_keysource.py
git commit -m "feat(secrets): get_master_key() reads HARBOR_CLERK_MASTER_KEY env var"
```

---

## Task 7: Cipher singleton accessor wired through Settings

**Files:**
- Modify: `src/harbor_clerk/secrets/__init__.py`
- Test: `tests/secrets/test_get_cipher.py`

The goal of this task: callers (the API route that creates a `mail_account`, the sync engine that decrypts to log in) shouldn't have to construct a `Cipher` from `get_master_key()` themselves every time. Provide one process-wide accessor that lazy-initializes on first call. This is also where any future "is the cipher healthy?" check would live.

- [ ] **Step 1: Write the failing test**

```python
# tests/secrets/test_get_cipher.py
"""Process-wide cipher accessor."""

import base64
import os

import pytest


@pytest.fixture(autouse=True)
def reset_cipher_singleton():
    """Each test gets a fresh accessor — clear the module cache."""
    from harbor_clerk.secrets import _accessor

    _accessor.reset()
    yield
    _accessor.reset()


def test_get_cipher_returns_singleton(monkeypatch):
    from harbor_clerk.secrets import get_cipher

    raw = os.urandom(32)
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", base64.b64encode(raw).decode())
    c1 = get_cipher()
    c2 = get_cipher()
    assert c1 is c2  # same instance
    assert c1.fingerprint == c2.fingerprint


def test_get_cipher_raises_when_key_missing(monkeypatch):
    from harbor_clerk.secrets import MissingMasterKey, get_cipher

    monkeypatch.delenv("HARBOR_CLERK_MASTER_KEY", raising=False)
    with pytest.raises(MissingMasterKey):
        get_cipher()


def test_get_cipher_round_trip(monkeypatch):
    from harbor_clerk.secrets import get_cipher

    monkeypatch.setenv(
        "HARBOR_CLERK_MASTER_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    cipher = get_cipher()
    ciphertext, fp = cipher.encrypt(b"top secret")
    assert cipher.decrypt(ciphertext, fp) == b"top secret"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/secrets/test_get_cipher.py -v`
Expected: FAIL — `ImportError: cannot import name '_accessor'` (or similar).

- [ ] **Step 3: Implement the accessor**

Create a new internal module `src/harbor_clerk/secrets/_accessor.py`:

```python
# src/harbor_clerk/secrets/_accessor.py
"""Process-wide singleton accessor for the Cipher.

Lazy-initialized on first call to keep import-time side-effects to zero
(useful for Alembic migrations, doc generation, etc. that import models
without needing to encrypt anything).
"""

from __future__ import annotations

from harbor_clerk.secrets.cipher import Cipher
from harbor_clerk.secrets.keysource import get_master_key

_cipher: Cipher | None = None


def get() -> Cipher:
    global _cipher
    if _cipher is None:
        _cipher = Cipher(get_master_key())
    return _cipher


def reset() -> None:
    """Drop the cached instance. For tests only."""
    global _cipher
    _cipher = None
```

Then expose it from the package init:

```python
# src/harbor_clerk/secrets/__init__.py — UPDATED
"""Envelope-encryption primitives for secret storage.

Postgres holds Fernet ciphertext for sensitive values (mail-account app
passwords today; potentially OAuth refresh tokens, off-host MinIO keys, etc.
later). The master key lives in the HARBOR_CLERK_MASTER_KEY env var on
every platform — operator sets it on Docker; macOS Swift reads from
Keychain and exports it on the Python subprocess environment.

Every ciphertext is paired with an 8-byte key fingerprint so cross-deployment
moves degrade gracefully (raise KeyMismatch instead of producing garbage).

Use `get_cipher()` to get the process-wide Cipher instance — it lazy-loads
the master key on first call.
"""

from harbor_clerk.secrets import _accessor
from harbor_clerk.secrets.cipher import Cipher, KeyMismatch
from harbor_clerk.secrets.keysource import MissingMasterKey, get_master_key


def get_cipher() -> Cipher:
    """Return the process-wide Cipher. Lazy-initialized on first call."""
    return _accessor.get()


__all__ = ["Cipher", "KeyMismatch", "MissingMasterKey", "get_cipher", "get_master_key"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/secrets/test_get_cipher.py -v`
Expected: PASS — three tests green.

- [ ] **Step 5: Run all secrets tests together**

Run: `uv run pytest tests/secrets/ -v`
Expected: PASS — every test in tests/secrets/ green.

- [ ] **Step 6: Commit**

```bash
git add src/harbor_clerk/secrets/_accessor.py src/harbor_clerk/secrets/__init__.py tests/secrets/test_get_cipher.py
git commit -m "feat(secrets): get_cipher() process-wide accessor"
```

---

## Task 8: Swift MasterKeyManager — Keychain CRUD

**Files:**
- Create: `macos/HarborClerkServer/HarborClerkServer/MasterKeyManager.swift`
- Create: `macos/HarborClerkServer/HarborClerkServerTests/MasterKeyManagerTests.swift`

The goal: a small Swift type that owns reading, generating, and storing a 32-byte master key in the user's login Keychain under a stable service identifier. Mirrors the existing pattern in `HarborClerk/KeychainManager.swift` (different bundle, different service id) but is purpose-built for binary key material rather than email/password credentials.

- [ ] **Step 1: Write the failing test**

```swift
// macos/HarborClerkServer/HarborClerkServerTests/MasterKeyManagerTests.swift
import XCTest
@testable import HarborClerkServer

final class MasterKeyManagerTests: XCTestCase {

    /// Use a unique service id per test so concurrent runs don't trample each other.
    private func makeManager() -> MasterKeyManager {
        let id = "com.harborclerk.test.\(UUID().uuidString)"
        return MasterKeyManager(serviceIdentifier: id)
    }

    override func tearDown() {
        // Each makeManager() uses a unique id, so leaks between tests are bounded;
        // explicit cleanup happens inside each test.
        super.tearDown()
    }

    func test_load_returns_nil_when_no_key_stored() {
        let manager = makeManager()
        defer { manager.delete() }
        XCTAssertNil(manager.load())
    }

    func test_generate_and_load_returns_32_bytes() {
        let manager = makeManager()
        defer { manager.delete() }

        let generated = manager.generate()
        XCTAssertEqual(generated.count, 32)

        let loaded = manager.load()
        XCTAssertEqual(loaded, generated)
    }

    func test_generate_is_idempotent_via_loadOrGenerate() {
        let manager = makeManager()
        defer { manager.delete() }

        let first = manager.loadOrGenerate()
        let second = manager.loadOrGenerate()
        XCTAssertEqual(first, second, "loadOrGenerate must reuse the existing key")
    }

    func test_delete_removes_the_key() {
        let manager = makeManager()
        _ = manager.generate()
        XCTAssertNotNil(manager.load())

        manager.delete()
        XCTAssertNil(manager.load())
    }

    func test_base64_encoded_is_44_chars() {
        let manager = makeManager()
        defer { manager.delete() }

        let key = manager.loadOrGenerate()
        let encoded = manager.base64Encoded(key: key)
        // 32 bytes → 44 chars base64 (with padding)
        XCTAssertEqual(encoded.count, 44)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

In Xcode (or via `xcodebuild`):

```bash
xcodebuild test \
    -project macos/HarborClerkServer/HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -destination 'platform=macOS' \
    -only-testing:HarborClerkServerTests/MasterKeyManagerTests
```

Expected: FAIL — "Cannot find 'MasterKeyManager' in scope".

- [ ] **Step 3: Create `MasterKeyManager.swift`**

```swift
// macos/HarborClerkServer/HarborClerkServer/MasterKeyManager.swift
import Foundation
import Security

/// Owns the master encryption key in the user's login Keychain.
///
/// The master key is 32 random bytes generated via SecRandomCopyBytes on
/// first launch. Only the HarborClerkServer (menubar) app reads/writes it —
/// the four Python subprocesses receive it as the HARBOR_CLERK_MASTER_KEY
/// env var, which inherits naturally to children and never touches disk.
///
/// Stored under a unique Keychain service identifier so multiple installs
/// (development, release) don't trample each other. Production uses
/// `MasterKeyManager.production`; tests pass a unique id per run.
final class MasterKeyManager {
    static let production = MasterKeyManager(serviceIdentifier: "com.bitblot.harborclerk.master-key")

    private let serviceIdentifier: String
    private let account = "master-key"

    init(serviceIdentifier: String) {
        self.serviceIdentifier = serviceIdentifier
    }

    /// Read the stored key, or nil if no key is stored.
    func load() -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceIdentifier,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return data
    }

    /// Generate a new 32-byte key and persist it. Overwrites any existing key.
    @discardableResult
    func generate() -> Data {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        precondition(status == errSecSuccess, "SecRandomCopyBytes failed: \(status)")
        let data = Data(bytes)
        store(data)
        return data
    }

    /// Read the stored key, generating a new one on first use.
    /// This is the normal entry point for app startup.
    func loadOrGenerate() -> Data {
        if let existing = load() {
            return existing
        }
        return generate()
    }

    /// Remove the stored key. Use only for testing or operator-initiated reset.
    func delete() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceIdentifier,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }

    /// Convenience: return the key as the standard base64 the env var expects.
    func base64Encoded(key: Data) -> String {
        return key.base64EncodedString()
    }

    // MARK: - Internals

    private func store(_ data: Data) {
        // Delete-then-add is the standard idiom for "set a Keychain item".
        delete()
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceIdentifier,
            kSecAttrAccount as String: account,
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlocked,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        precondition(status == errSecSuccess, "Failed to store master key in Keychain: \(status)")
    }
}
```

- [ ] **Step 4: Add the file to the Xcode project**

Open `macos/HarborClerkServer/HarborClerkServer.xcodeproj` in Xcode and drag `MasterKeyManager.swift` into the `HarborClerkServer` target. Drag the test file into the `HarborClerkServerTests` target.

(If you can't open Xcode in the agent context, do this via `xcodeproj` Ruby gem or by editing `project.pbxproj` directly — but the GUI flow is documented for the human reviewer.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
xcodebuild test \
    -project macos/HarborClerkServer/HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -destination 'platform=macOS' \
    -only-testing:HarborClerkServerTests/MasterKeyManagerTests
```

Expected: PASS — all five tests green.

- [ ] **Step 6: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/MasterKeyManager.swift \
        macos/HarborClerkServer/HarborClerkServerTests/MasterKeyManagerTests.swift \
        macos/HarborClerkServer/HarborClerkServer.xcodeproj/project.pbxproj
git commit -m "feat(macos): MasterKeyManager — Keychain CRUD for master encryption key"
```

---

## Task 9: First-launch master-key bootstrap in HarborClerkServer

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift`

The goal: on every launch, ensure the master key exists in Keychain (creating it if this is a first launch). The existing `AppDelegate.applicationDidFinishLaunching` is the right hook — it runs before `ServiceManager.startAll()`.

- [ ] **Step 1: Read `AppDelegate.swift` to find the launch hook**

```bash
cat macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift
```

Look for `applicationDidFinishLaunching` or whatever method `ServiceManager.startAll()` is called from. The bootstrap call must happen *before* services start.

- [ ] **Step 2: Add the bootstrap call**

In `AppDelegate.swift`, before the call that triggers `ServiceManager.startAll()`, add:

```swift
// First-launch master key bootstrap. Idempotent — generates the key in
// Keychain only on the very first launch; subsequent launches just read.
// Must run before ServiceManager.startAll() because the Python subprocesses
// need this key in their environment.
_ = MasterKeyManager.production.loadOrGenerate()
```

(Discarding the return value is intentional — we only care about the side effect of ensuring the key exists. ServiceManager will read it again when constructing the env for subprocesses in Task 10.)

- [ ] **Step 3: Verify the existing app-startup tests still pass**

```bash
xcodebuild test \
    -project macos/HarborClerkServer/HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -destination 'platform=macOS' \
    -only-testing:HarborClerkServerTests/OverallStateTests \
    -only-testing:HarborClerkServerTests/HealthCheckerTests
```

Expected: PASS — bootstrap call is a no-op on second launch and the existing tests don't exercise Keychain.

- [ ] **Step 4: Manual smoke check (run the app)**

```bash
open macos/HarborClerkServer/build/Debug/HarborClerkServer.app  # or whatever the build path is
```

Then in Terminal:

```bash
security find-generic-password -s 'com.bitblot.harborclerk.master-key' -w
```

Expected: a 44-character base64 string is printed (the key as base64). The first run generates it; the second run reads the same one.

If you see "The specified item could not be found in the keychain.", the bootstrap call didn't run — re-check that you put it before `ServiceManager.startAll()`.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift
git commit -m "feat(macos): bootstrap master key in Keychain on first launch"
```

---

## Task 10: Pass HARBOR_CLERK_MASTER_KEY to Python subprocesses

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift:880-895`

The goal: every Python subprocess (api, worker-io, worker-cpu, watcher) launched by `ServiceManager` gets `HARBOR_CLERK_MASTER_KEY` in its environment. The current code already builds an env dict around line 880-895 (where we saw `"NATIVE_CONFIG_FILE": settings.configURL.path`); we just add one more entry.

- [ ] **Step 1: Read the current env-construction code**

```bash
sed -n '870,910p' macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift
```

Identify the `[String: String]` env dictionary that's passed to subprocesses. There may be more than one (one per service); add the env var to *each* one used to launch a Python service.

- [ ] **Step 2: Add the env var**

In the env-construction code (likely a function or computed property used by every Python service launch), add:

```swift
// Master encryption key for secret storage. See MasterKeyManager and the
// Python harbor_clerk.secrets module. Read from Keychain and exported
// as the same env var that Docker operators set directly.
let masterKey = MasterKeyManager.production.loadOrGenerate()
env["HARBOR_CLERK_MASTER_KEY"] = masterKey.base64EncodedString()
```

If the env is built in a single helper used by all services, one place to change. If it's built per-service inline, add to each (api, worker-io, worker-cpu, watcher).

- [ ] **Step 3: Verify the env is set on subprocess launch**

Add a test (or a manual check) that confirms the env reaches Python. Easiest manual check: temporarily add a logger line to `harbor_clerk/secrets/keysource.py:get_master_key()` like `logger.info("master key loaded, fingerprint=%s", ...)`, run the app, and check the worker log:

```bash
tail -f ~/Library/Application\ Support/Harbor\ Clerk/logs/worker-io.log
```

Expected: a line confirming master key load. Remove the temporary log line before committing.

For an automated test, write a Swift test that constructs ServiceManager (or whichever helper it uses), inspects the resulting env dict, and asserts `HARBOR_CLERK_MASTER_KEY` is present and matches the Keychain value:

```swift
// Append to MasterKeyManagerTests.swift or a new file ServiceManagerEnvTests.swift
func test_subprocess_env_contains_master_key() {
    // Implementation depends on how ServiceManager is structured —
    // if there's a public/internal `subprocessEnv()` method, call it
    // and assert the key is present. If env construction is inline,
    // refactor it into a method first to make it testable.
    let env = ServiceManager.shared.subprocessEnv()
    let key = env["HARBOR_CLERK_MASTER_KEY"]
    XCTAssertNotNil(key)
    let decoded = Data(base64Encoded: key!)
    XCTAssertEqual(decoded?.count, 32)
}
```

(If ServiceManager's env construction isn't currently extracted into a callable method, do that refactor as part of this task — it's a small and worthwhile testability improvement.)

- [ ] **Step 4: Run the test**

```bash
xcodebuild test \
    -project macos/HarborClerkServer/HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -destination 'platform=macOS'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift \
        macos/HarborClerkServer/HarborClerkServerTests/   # whichever test file you added/modified
git commit -m "feat(macos): inject HARBOR_CLERK_MASTER_KEY into Python subprocess env"
```

---

## Task 11: Operator documentation

**Files:**
- Create: `docs/secrets-and-keys.md`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `docs/architecture.md`

The goal: an operator opening this project for the first time understands what the master key is, how to set it (Docker), what happens on macOS (Keychain), and what to do if they lose it.

- [ ] **Step 1: Create `docs/secrets-and-keys.md`**

```markdown
# Secrets and the master key

Harbor Clerk encrypts sensitive values (currently mail-account app passwords; in
the future, OAuth refresh tokens, off-host MinIO keys, and similar) with a
master key before storing them in Postgres. This document explains how the
master key works on each deployment, how to back it up, and what to do if you
lose it.

## How the master key works

- The master key is **32 random bytes**, base64-encoded for transport.
- Every encrypted column in Postgres stores `(ciphertext, key_fingerprint)`. The
  fingerprint is an HMAC-SHA256 of the master key, truncated to 8 bytes — it
  identifies which key was used without revealing the key itself.
- On decrypt, the fingerprint is compared to the active key's fingerprint
  before any decryption is attempted. A mismatch raises `KeyMismatch`, which
  the application surfaces in System Status as "reconnect mail accounts".

## Where the master key lives

### Docker

Operator sets `HARBOR_CLERK_MASTER_KEY` in the Compose env. Generate one:

```bash
python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
```

Add it to `.env` or your secrets workflow:

```dotenv
HARBOR_CLERK_MASTER_KEY=YOUR_BASE64_KEY_HERE
```

The `app`, `worker-io`, `worker-cpu`, and `watcher` services all need this env
var. `docker-compose.yml` documents this on each service.

**Back this up.** A Postgres backup without the master key is useless for any
encrypted column. The simplest workflow: store the env var alongside your
Postgres backup credentials in your secrets manager.

### macOS native

The Harbor Clerk Server menubar app generates the master key on first launch
and stores it in your login Keychain under the service identifier
`com.bitblot.harborclerk.master-key`. On every subsequent launch, Swift reads
it from Keychain and exports it as `HARBOR_CLERK_MASTER_KEY` to all Python
subprocesses (api, worker-io, worker-cpu, watcher).

**You don't need to do anything.** The key is generated, persisted, and rotated
into subprocesses automatically.

**To inspect the key** (for backup or debugging):

```bash
security find-generic-password -s 'com.bitblot.harborclerk.master-key' -w
```

This prints the 44-character base64 representation. Keep it somewhere safe if
you intend to migrate this DB to another machine or to Docker.

**To back it up**, copy that base64 string into a secure location (1Password,
your secrets manager, an encrypted note). Time Machine backs up Keychain by
default, but only restoring to the *same user account* on the *same machine*
restores Keychain access — cross-machine moves require manual export.

## What happens if you lose the master key

You'll see "X mail accounts need reconnecting" in System Status. The encrypted
secrets are unrecoverable, but everything else is intact (documents, chunks,
embeddings, search history, audit logs are all unencrypted).

The fix: click each affected mail account and re-paste the app password. New
ciphertexts are tagged with the new master key's fingerprint and the account
becomes active again.

## Migrating between deployments

### Docker → Docker (same key)

Carry `HARBOR_CLERK_MASTER_KEY` over to the new deployment. Restore Postgres.
Mail accounts work without intervention.

### Docker → macOS

Two options.

**Option A — Re-enter (simplest):** Restore Postgres. Launch the macOS app.
Status shows "X mail accounts need reconnecting." Re-paste app passwords.

**Option B — Import the master key (no re-entry):** *Not yet implemented.* See
the email-ingestion spec; an "Import master key" admin form is planned for
Stage 4. Until that ships, Option A is the only path.

### macOS → Docker

Same shape as the reverse. Export the macOS Keychain key with `security
find-generic-password -w`, set it as `HARBOR_CLERK_MASTER_KEY` on the Docker
side, restore Postgres.

## Key rotation

Not implemented in MVP. When it becomes necessary, the design is: a maintenance
command iterates every encrypted column, decrypts with the old key, re-encrypts
with the new key, updates the fingerprint. Until then, treat the master key as
generate-once-and-keep-forever.
```

- [ ] **Step 2: Update `docker-compose.yml`**

Add `HARBOR_CLERK_MASTER_KEY` to the env block of `app`, `worker-io`, `worker-cpu`, and (when it exists in compose) `watcher`. Pattern:

```yaml
services:
  app:
    environment:
      # ... existing entries ...
      - HARBOR_CLERK_MASTER_KEY=${HARBOR_CLERK_MASTER_KEY:?master key required; see docs/secrets-and-keys.md}
```

The `:?` syntax causes Compose to fail loudly if the env var is unset, with the message after `?` shown to the operator. Repeat for every Python service.

- [ ] **Step 3: Update `.env.example`**

Add a documented entry near the top:

```dotenv
# Master encryption key for secret storage (32 random bytes, base64).
# Generate with:
#   python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
# See docs/secrets-and-keys.md for the lifecycle.
HARBOR_CLERK_MASTER_KEY=
```

- [ ] **Step 4: Add a one-line link in `docs/architecture.md`**

Find an appropriate section (likely near the storage / config description) and add:

```markdown
- **Secret storage** — sensitive values (mail-account app passwords, future
  OAuth tokens, etc.) are encrypted in Postgres with a master key managed per
  deployment. See [secrets-and-keys.md](secrets-and-keys.md).
```

- [ ] **Step 5: Verify docker-compose still parses**

```bash
docker compose config > /dev/null
```

Expected: no parse errors, but it WILL exit non-zero with the `master key required` message if `HARBOR_CLERK_MASTER_KEY` is unset — that's the desired behavior. Set the env var temporarily to verify:

```bash
HARBOR_CLERK_MASTER_KEY=$(python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())') docker compose config > /dev/null && echo "OK"
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add docs/secrets-and-keys.md docker-compose.yml .env.example docs/architecture.md
git commit -m "docs(secrets): operator guide for master key — Docker env var, macOS Keychain, recovery"
```

---

## Task 12: End-to-end smoke test of the foundation

**Files:**
- Create: `tests/secrets/test_end_to_end_smoke.py`

The goal: encrypt a value with the cipher, persist it to a `mail_accounts` row, fetch it back, decrypt — all the foundation pieces talking to each other in one test. This catches integration regressions if any later refactor breaks the chain.

- [ ] **Step 1: Write the failing test**

```python
# tests/secrets/test_end_to_end_smoke.py
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
        await db_session.execute(
            select(MailAccount).where(MailAccount.imap_username == "roundtrip@example.com")
        )
    ).scalar_one()

    decrypted = cipher.decrypt(fetched.app_password_ciphertext, fetched.key_fingerprint)
    assert decrypted == plaintext_password
```

- [ ] **Step 2: Run test to verify it passes**

Run: `uv run pytest tests/secrets/test_end_to_end_smoke.py -v`
Expected: PASS — round-trip works through the DB.

- [ ] **Step 3: Run the full test suite to catch any regressions**

Run: `uv run pytest -v`
Expected: PASS — all tests green, including pre-existing ones.

- [ ] **Step 4: Run linting**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: PASS — no lint or format issues.

- [ ] **Step 5: Commit**

```bash
git add tests/secrets/test_end_to_end_smoke.py
git commit -m "test(secrets): end-to-end smoke — cipher → DB → fetch → decrypt"
```

---

## Wrap-up

After Task 12 commits, the foundation is complete. To verify the end-to-end story:

- [ ] **Run the full Python test suite**

```bash
uv run pytest -v
```

Expected: every test green.

- [ ] **Run the full Swift test suite**

```bash
xcodebuild test \
    -project macos/HarborClerkServer/HarborClerkServer.xcodeproj \
    -scheme HarborClerkServer \
    -destination 'platform=macOS'
```

Expected: every Swift test green.

- [ ] **Run the security scan to confirm `cryptography` made it into pip-audit's view**

```bash
uv run pip-audit --desc 2>&1 | tail -20
```

Expected: no HIGH-severity findings; `cryptography` listed as a known dep.

- [ ] **Run the migration on a fresh DB**

```bash
# Spin up a clean Postgres
docker compose up -d postgres
# Migrate
DATABASE_URL=postgresql+asyncpg://lka:lka_dev_password@localhost:5432/lka uv run alembic upgrade head
```

Expected: migration 0020 applies cleanly; `\d mail_accounts` in psql shows the expected shape.

- [ ] **Stage 1 done. Open the PR.**

```bash
gh pr create --title "feat(email): stage 1 foundation — schema + secrets + Keychain bootstrap" \
    --body-file <(cat <<'EOF'
## Summary

Foundation for email ingestion via IMAP labels. No user-visible behavior; lays groundwork for stages 2-4.

- Schema: `mail_accounts`, `watched_labels`, `watched_messages`, plus nullable email-metadata columns on `documents` (alembic 0020).
- `harbor_clerk.secrets` module: Fernet envelope encryption with key fingerprinting; `KeyMismatch` exception for cross-deployment portability; process-wide `get_cipher()` accessor.
- macOS Swift: `MasterKeyManager` reads/generates a 32-byte master key in the user's login Keychain; injected into every Python subprocess as `HARBOR_CLERK_MASTER_KEY`.
- Docker: operator sets `HARBOR_CLERK_MASTER_KEY` directly. Same env var name across both deployments → one Python code path.
- Operator docs at [docs/secrets-and-keys.md](docs/secrets-and-keys.md).

Spec: [`docs/superpowers/specs/2026-05-04-email-ingestion-design.md`](docs/superpowers/specs/2026-05-04-email-ingestion-design.md). Plan: [`docs/superpowers/plans/2026-05-04-email-ingestion-stage-1-foundation.md`](docs/superpowers/plans/2026-05-04-email-ingestion-stage-1-foundation.md).

## Test plan

- [x] All Python tests pass: `uv run pytest -v`
- [x] All Swift tests pass: `xcodebuild test -project macos/HarborClerkServer/HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS'`
- [x] Migration applies cleanly on a fresh DB
- [x] `pip-audit` clean
- [x] Manual: macOS app launch generates Keychain entry, second launch reads it
- [x] Manual: Docker compose with `HARBOR_CLERK_MASTER_KEY` set starts cleanly; without it, fails loudly

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```
