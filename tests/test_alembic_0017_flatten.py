"""Test the 0017 flatten-document-model migration."""

import hashlib
import os
import uuid

import pytest
import sqlalchemy.exc
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command

# Sync URL for Alembic (which runs its own event loop)
_SYNC_URL = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")


@pytest.fixture(scope="module")
def alembic_cfg():
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])
    return cfg


@pytest.fixture(scope="module")
def sync_engine():
    engine = create_engine(_SYNC_URL)
    yield engine
    engine.dispose()


def test_0017_flattens_two_version_doc(alembic_cfg, sync_engine):
    """A doc with two versions: latest content survives, prior is pruned."""
    # Ensure we are exactly at revision 0016 before inserting test data.
    # Go to base first (guaranteed clean state), then upgrade to 0016.
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0016")

    doc_id = uuid.uuid4()
    v1_id = uuid.uuid4()
    v2_id = uuid.uuid4()
    v1_sha = hashlib.sha256(b"old").digest()
    v2_sha = hashlib.sha256(b"new").digest()

    with sync_engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO documents (doc_id, title, status, latest_version_id, created_at, updated_at)
            VALUES (:doc_id, 'Test', 'active', :v2_id, now(), now())
        """),
            {"doc_id": doc_id, "v2_id": v2_id},
        )
        conn.execute(
            text("""
            INSERT INTO document_versions
                (version_id, doc_id, original_sha256, status, summary, mime_type, doc_type,
                 has_text_layer, needs_ocr, extracted_chars, source_path, size_bytes,
                 original_bucket, original_object_key, summary_model, error,
                 created_at, updated_at)
            VALUES (:vid, :did, :sha, 'ready', :summary, 'application/pdf', 'contract',
                    true, false, 1000, '/path/old.pdf', 50000,
                    'originals', :okey, 'qwen', NULL,
                    now(), now())
        """),
            {
                "vid": v1_id,
                "did": doc_id,
                "sha": v1_sha,
                "summary": "old summary",
                "okey": f"originals/versions/{v1_id}/old.pdf",
            },
        )
        conn.execute(
            text("""
            INSERT INTO document_versions
                (version_id, doc_id, original_sha256, status, summary, mime_type, doc_type,
                 has_text_layer, needs_ocr, extracted_chars, source_path, size_bytes,
                 original_bucket, original_object_key, summary_model, error,
                 created_at, updated_at)
            VALUES (:vid, :did, :sha, 'ready', :summary, 'application/pdf', 'contract',
                    true, false, 1100, '/path/new.pdf', 51000,
                    'originals', :okey, 'qwen', NULL,
                    now(), now())
        """),
            {
                "vid": v2_id,
                "did": doc_id,
                "sha": v2_sha,
                "summary": "new summary",
                "okey": f"originals/versions/{v2_id}/new.pdf",
            },
        )
        # One chunk per version so we can assert pruning
        conn.execute(
            text("""
            INSERT INTO chunks (chunk_id, doc_id, version_id, chunk_num, chunk_text,
                                 char_start, char_end, page_start, page_end, language,
                                 created_at)
            VALUES (gen_random_uuid(), :did, :vid, 0, 'old text',
                    0, 8, 1, 1, 'english',
                    now())
        """),
            {"did": doc_id, "vid": v1_id},
        )
        conn.execute(
            text("""
            INSERT INTO chunks (chunk_id, doc_id, version_id, chunk_num, chunk_text,
                                 char_start, char_end, page_start, page_end, language,
                                 created_at)
            VALUES (gen_random_uuid(), :did, :vid, 0, 'new text',
                    0, 8, 1, 1, 'english',
                    now())
        """),
            {"did": doc_id, "vid": v2_id},
        )

    # Run the migration under test
    command.upgrade(alembic_cfg, "0017")

    with sync_engine.begin() as conn:
        # Document table got the new columns populated from v2
        row = conn.execute(
            text("""
            SELECT sha256, summary, doc_type, mime_type, source_path, size_bytes,
                   has_text_layer, needs_ocr, extracted_chars, original_object_key,
                   pipeline_status, pipeline_seq
            FROM documents WHERE doc_id = :did
        """),
            {"did": doc_id},
        ).first()
        assert bytes(row.sha256) == v2_sha
        assert row.summary == "new summary"
        assert row.source_path == "/path/new.pdf"
        assert row.size_bytes == 51000
        assert row.original_object_key == f"originals/docs/{doc_id}/new.pdf"
        assert row.pipeline_status == "ready"
        assert row.pipeline_seq == 0

        # Only the v2 chunk survived
        chunks = conn.execute(
            text("SELECT chunk_text FROM chunks WHERE doc_id = :did"),
            {"did": doc_id},
        ).fetchall()
        assert len(chunks) == 1
        assert chunks[0].chunk_text == "new text"

    # document_versions table is gone — use a separate connection so the
    # expected error doesn't poison the transaction for the next assertion.
    with sync_engine.connect() as probe_conn, pytest.raises(sqlalchemy.exc.ProgrammingError):
        probe_conn.execute(text("SELECT 1 FROM document_versions LIMIT 1"))

    with sync_engine.begin() as conn:
        # latest_version_id column is gone
        cols = conn.execute(
            text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'documents'
        """)
        ).fetchall()
        assert "latest_version_id" not in {c.column_name for c in cols}
