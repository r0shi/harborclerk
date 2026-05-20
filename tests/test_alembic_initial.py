"""Smoke test: the rebased 0001_initial.py creates the expected schema.

Task 5 collapsed the 0001-0023 alembic chain into a single 0001_initial.py.
These tests verify the rebased migration produces the embedding-v2 schema:
chunks.embedding at vector(768) and the schema_metadata sentinel rows.
"""

import subprocess

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_chunks_embedding_is_768_dim(db_session):
    """The rebased migration creates chunks.embedding as vector(768)."""
    result = await db_session.execute(
        text(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
    )
    assert result.scalar_one() == "vector(768)"


@pytest.mark.asyncio
async def test_schema_metadata_sentinel_rows(db_session):
    """The rebased migration seeds the three sentinel rows."""
    result = await db_session.execute(text("SELECT key, value FROM schema_metadata ORDER BY key"))
    rows = dict(result.all())
    assert rows == {
        "embed_model": "ibm-granite/granite-embedding-311m-multilingual-r2",
        "embed_dim": "768",
        "reranker": "bge-reranker-v2-m3",
    }


@pytest.mark.asyncio
async def test_users_email_is_citext(db_session):
    """users.email must stay citext (case-insensitive) — a regression the
    rebase autogenerate would otherwise have silently downgraded to text."""
    result = await db_session.execute(
        text(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'users'::regclass AND attname = 'email'"
        )
    )
    assert result.scalar_one() == "citext"


@pytest.mark.asyncio
async def test_watched_files_folder_path_unique(db_session):
    """watched_files keeps its (folder_id, relative_path) uniqueness."""
    result = await db_session.execute(
        text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'watched_files'::regclass "
            "AND contype = 'u' "
            "AND pg_get_constraintdef(oid) = 'UNIQUE (folder_id, relative_path)'"
        )
    )
    assert result.scalar_one() == 1


def test_alembic_history_has_single_revision():
    """The rebase squashed the 0001-0023 chain into one revision."""
    result = subprocess.run(
        ["uv", "run", "alembic", "history"],
        capture_output=True,
        text=True,
        check=True,
    )
    # `alembic history` prints one line per revision edge; a single-revision
    # chain has exactly one line, formatted "<base> -> 0001_initial (head), ...".
    lines = [ln for ln in result.stdout.splitlines() if "->" in ln]
    assert len(lines) == 1, f"expected 1 revision, got history:\n{result.stdout}"
    assert "0001_initial" in lines[0]
