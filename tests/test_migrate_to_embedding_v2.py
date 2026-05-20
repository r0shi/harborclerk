"""Tests for the external migration script (migrate_to_embedding_v2.py).

Uses scratch databases loaded from tests/fixtures/pre_embedding_v2_schema.sql
rather than the shared harbor_clerk_test DB. Each test creates and drops its
own scratch DB so they're fully isolated and never touch the live DB.

Postgres access: localhost:5433, user lka (the macOS app's bundled PG).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "migrate_to_embedding_v2.py"
FIXTURE_SQL = Path(__file__).parent / "fixtures" / "pre_embedding_v2_schema.sql"
PG_HOST = "localhost"
PG_PORT = 5433
PG_USER = "lka"

# Resolve psql: prefer PATH, fall back to bundled macOS app binary.
_BUNDLED_PSQL = Path(
    "/Users/alex/mcp-gateway/macos/build/output/HarborClerkServer.app/Contents/Resources/postgres/bin/psql"
)
_psql_which = shutil.which("psql")
if _psql_which:
    PSQL: Path | None = Path(_psql_which)
elif _BUNDLED_PSQL.exists():
    PSQL = _BUNDLED_PSQL
else:
    PSQL = None

pytestmark = pytest.mark.skipif(PSQL is None, reason="psql not found on PATH or bundled macOS path")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


def _pg_url(dbname: str) -> str:
    return f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{dbname}"


def _create_scratch_db(dbname: str) -> None:
    """Create a named scratch DB, load the pre-rebase fixture, insert alembic_version row."""
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname="postgres")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        cur.execute(f'CREATE DATABASE "{dbname}"')
    conn.close()

    # Load schema fixture via psql subprocess (handles \restrict psql-private directives)
    subprocess.run(
        [str(PSQL), "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, "-d", dbname, "-f", str(FIXTURE_SQL)],
        check=True,
        capture_output=True,
    )

    # pg_dump --schema-only does not include row data; insert the alembic_version sentinel
    conn2 = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=dbname)
    conn2.autocommit = True
    with conn2.cursor() as cur:
        cur.execute("INSERT INTO alembic_version (version_num) VALUES ('0023')")
    conn2.close()


def _drop_scratch_db(dbname: str) -> None:
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname="postgres")
    conn.autocommit = True
    with conn.cursor() as cur:
        # Terminate any lingering connections (e.g. the script left one open after a refusal)
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (dbname,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    conn.close()


@pytest.fixture
def scratch_db(request):
    """Fixture: create a pre-rebase scratch DB, yield its name, drop it on teardown."""
    # Use the test node id to derive a unique DB name (strip non-alphanumeric chars)
    safe_name = "".join(c if c.isalnum() else "_" for c in request.node.name)[:40]
    dbname = f"migrate_test_{safe_name}"
    _create_scratch_db(dbname)
    yield dbname
    _drop_scratch_db(dbname)


# ---------------------------------------------------------------------------
# Pre-flight refusal tests
# ---------------------------------------------------------------------------


def test_script_refuses_without_confirm(scratch_db):
    """Missing --confirm flag must cause non-zero exit and mention --confirm."""
    r = _run("--db-url", _pg_url(scratch_db))
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "--confirm" in combined


def test_script_refuses_without_db_url():
    """Missing --db-url must cause non-zero exit (argparse error)."""
    r = _run("--confirm")
    assert r.returncode != 0


def test_script_refuses_when_sentinel_already_set(scratch_db):
    """If schema_metadata + embed_model row already exist, script must refuse."""
    # Manually insert the sentinel table + row to simulate an already-migrated DB
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=scratch_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL, set_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        cur.execute(
            "INSERT INTO schema_metadata (key, value) VALUES ('embed_model', 'ibm-granite/granite-embedding-311m-multilingual-r2')"
        )
    conn.close()

    r = _run("--db-url", _pg_url(scratch_db), "--confirm")
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "already" in combined.lower() or "sentinel" in combined.lower()


def test_script_refuses_when_alembic_at_wrong_head(scratch_db):
    """If alembic_version != '0023', script must refuse and mention the expected revision."""
    # Change alembic_version to something unexpected
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=scratch_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE alembic_version SET version_num = '0019'")
    conn.close()

    r = _run("--db-url", _pg_url(scratch_db), "--confirm")
    assert r.returncode != 0
    combined = r.stdout + r.stderr
    assert "0023" in combined


# ---------------------------------------------------------------------------
# Happy path test
# ---------------------------------------------------------------------------


def test_script_happy_path_against_pre_rebase_db(scratch_db):
    """Load pre-rebase fixture DB, run the script, verify post-state.

    The scratch_db fixture gives us:
    - schema_metadata does NOT exist
    - alembic_version = '0023'
    - chunks.embedding is vector(384)

    We seed one 'ready' doc so the re-enqueue logic has something to count.
    """
    # Seed one 'ready' document so the re-enqueue has something to count
    conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=scratch_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO documents (title, sha256, mime_type, pipeline_status, created_at, updated_at)
            VALUES ('test-doc', decode('deadbeef', 'hex'), 'text/plain', 'ready', NOW(), NOW())
            """
        )
    conn.close()

    r = _run("--db-url", _pg_url(scratch_db), "--confirm")
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

    # Verify post-state
    conn2 = psycopg2.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, dbname=scratch_db)
    conn2.autocommit = True
    with conn2.cursor() as cur:
        # sentinel row present
        cur.execute("SELECT value FROM schema_metadata WHERE key='embed_model'")
        row = cur.fetchone()
        assert row is not None, "schema_metadata.embed_model row missing after migration"
        assert row[0] == "ibm-granite/granite-embedding-311m-multilingual-r2"

        # embed_dim sentinel
        cur.execute("SELECT value FROM schema_metadata WHERE key='embed_dim'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "768"

        # chunks.embedding is now vector(768)
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
        col_row = cur.fetchone()
        assert col_row is not None, "chunks.embedding column missing after migration"
        assert col_row[0] == "vector(768)", f"Expected vector(768), got {col_row[0]}"

        # HNSW index rebuilt
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'chunks' AND indexname = 'chunks_embedding_hnsw_idx'"
        )
        assert cur.fetchone() is not None, "HNSW index not rebuilt after migration"

        # alembic_version updated
        cur.execute("SELECT version_num FROM alembic_version")
        revision = cur.fetchone()[0]
        assert revision == "0001_initial", f"Expected 0001_initial, got {revision}"

        # one embed job enqueued for the seeded ready doc
        cur.execute("SELECT COUNT(*) FROM ingestion_jobs WHERE stage='embed' AND status='queued'")
        job_count = cur.fetchone()[0]
        assert job_count == 1, f"Expected 1 embed job, got {job_count}"
    conn2.close()
