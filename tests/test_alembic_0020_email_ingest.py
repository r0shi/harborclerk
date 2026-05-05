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
    assert "ix_watched_messages_label_status" in index_names
    assert "ix_watched_messages_eml_sha" in index_names


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
    assert "ix_documents_email_message_id" in index_names
    assert "ix_documents_email_thread_id" in index_names
    assert "ix_documents_email_parent" in index_names
