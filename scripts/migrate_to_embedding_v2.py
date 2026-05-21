"""External migration script: existing-DB → embedding-v2 schema.

For operators upgrading an existing Harbor Clerk DB without wiping it.
Single transaction; idempotent pre-flight refusals prevent re-running or
running against an unexpected schema state.

Usage:
  uv run python scripts/migrate_to_embedding_v2.py \\
    --db-url postgresql://user:pass@host:port/dbname \\
    --confirm

See docs/upgrade-runbook.md#embedding-v2 for full operator runbook.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import psycopg2

# The single expected alembic version that this script knows how to migrate FROM.
# If the DB is at any other revision, the script refuses (we'd be guessing).
EXPECTED_PRE_REBASE_HEAD = "0023"

# The revision id of the rebased initial migration. Must match the
# `revision = "0001_initial"` line at the top of alembic/versions/0001_initial.py.
REBASED_INITIAL_REVISION = "0001_initial"

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(_log_path()),
        ],
    )


def _log_path() -> Path:
    import os

    if os.environ.get("HARBOR_CLERK_LOG_DIR"):
        return Path(os.environ["HARBOR_CLERK_LOG_DIR"]) / "migrate_to_embedding_v2.log"
    # macOS native default
    candidate = Path.home() / "Library/Application Support/Harbor Clerk/logs"
    if candidate.parent.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate / "migrate_to_embedding_v2.log"
    Path("./logs").mkdir(parents=True, exist_ok=True)
    return Path("./logs/migrate_to_embedding_v2.log")


def preflight_check(conn) -> None:
    """Raise RuntimeError with operator-actionable message on any check failure."""
    with conn.cursor() as cur:
        # Check 1: schema_metadata must not already exist with sentinel rows.
        # If it does, this DB has already been migrated to embedding-v2.
        cur.execute("SELECT to_regclass('schema_metadata')")
        exists = cur.fetchone()[0]
        if exists is not None:
            cur.execute("SELECT key FROM schema_metadata WHERE key='embed_model'")
            if cur.fetchone() is not None:
                raise RuntimeError(
                    "schema_metadata.embed_model row already exists — this DB has "
                    "already been migrated to embedding-v2. Aborting."
                )

        # Check 2: alembic_version must equal the expected pre-rebase head.
        # If the DB is at any other revision, we refuse — we'd be guessing.
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("alembic_version table is empty — DB is in an unexpected state. Aborting.")
        current = row[0]
        if current != EXPECTED_PRE_REBASE_HEAD:
            raise RuntimeError(
                f"alembic_version is '{current}', expected '{EXPECTED_PRE_REBASE_HEAD}'. "
                f"This script knows how to migrate only from the pre-rebase head. "
                f"If your DB is at a different revision, restore from backup or upgrade "
                f"to '{EXPECTED_PRE_REBASE_HEAD}' first."
            )

        # Check 3: chunks.embedding must be vector(384) — the pre-v2 dimension.
        cur.execute(
            "SELECT format_type(atttypid, atttypmod) "
            "FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        )
        col_row = cur.fetchone()
        if col_row is None or col_row[0] != "vector(384)":
            raise RuntimeError(
                f"Expected chunks.embedding to be vector(384); "
                f"found {col_row[0] if col_row else '<missing>'}. Aborting."
            )


def run_migration(conn) -> None:
    """Apply the embedding-v2 schema changes + enqueue re-embed jobs.

    Runs as a single transaction. The caller commits or rolls back.
    """
    with conn.cursor() as cur:
        logger.info("Creating schema_metadata table + sentinel rows")
        cur.execute(
            """
            CREATE TABLE schema_metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO schema_metadata (key, value) VALUES
              ('embed_model', 'ibm-granite/granite-embedding-311m-multilingual-r2'),
              ('embed_dim', '768'),
              ('reranker', 'bge-reranker-v2-m3')
            """
        )

        logger.info("Dropping + recreating chunks.embedding as vector(768)")
        cur.execute("ALTER TABLE chunks DROP COLUMN embedding")
        cur.execute("ALTER TABLE chunks ADD COLUMN embedding vector(768)")

        logger.info("Rebuilding chunks_embedding_hnsw_idx")
        cur.execute("DROP INDEX IF EXISTS chunks_embedding_hnsw_idx")
        cur.execute(
            "CREATE INDEX chunks_embedding_hnsw_idx ON chunks "
            "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )

        logger.info("Updating alembic_version → %s", REBASED_INITIAL_REVISION)
        cur.execute(
            "UPDATE alembic_version SET version_num = %s",
            (REBASED_INITIAL_REVISION,),
        )

        logger.info("Enqueueing embed-stage jobs for all ready docs (most-recent-first)")
        # DO UPDATE, not DO NOTHING: every ready doc already has a completed
        # (doc_id, 'embed') row from its original ingestion, so DO NOTHING would
        # skip all of them and re-embed nothing. Reset the existing rows to
        # 'queued' so workers re-run the stage against the now-empty 768-dim
        # column.
        cur.execute(
            """
            INSERT INTO ingestion_jobs (doc_id, stage, status, created_at)
            SELECT doc_id, 'embed', 'queued', NOW()
            FROM documents
            WHERE pipeline_status = 'ready'
            ORDER BY updated_at DESC
            ON CONFLICT (doc_id, stage) DO UPDATE SET
                status = 'queued',
                error = NULL,
                started_at = NULL,
                finished_at = NULL,
                heartbeat_at = NULL,
                progress_current = 0
            """
        )
        enqueued = cur.rowcount
        logger.info("Enqueued %d embed jobs", enqueued)


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(
        prog="migrate_to_embedding_v2",
        description="Migrate an existing Harbor Clerk DB to embedding-v2 schema.",
    )
    parser.add_argument(
        "--db-url",
        required=True,
        help="sync postgres URL: postgresql://user:pass@host:port/dbname",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        required=True,
        help="acknowledge destructive operation",
    )
    args = parser.parse_args(argv)

    logger.info("Connecting to %s", args.db_url.split("@")[-1])
    conn = psycopg2.connect(args.db_url)
    conn.autocommit = False
    try:
        try:
            preflight_check(conn)
        except RuntimeError as exc:
            logger.error("Pre-flight check failed: %s", exc)
            return 1

        run_migration(conn)
        conn.commit()
        logger.info("Migration complete. Re-launch HC from the embedding-v2 binary.")
        return 0
    except Exception:
        conn.rollback()
        logger.exception("Migration failed; transaction rolled back. DB is unchanged.")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
