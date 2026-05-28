"""Integration test for migration 0006 — backfill semantics, not Alembic plumbing.

The session-scoped `_engine` fixture runs the migration on an empty DB during
setup, which only proves it's syntactically valid. The migration's real work
is the per-extension grouped UPDATE, which only fires when NULL rows are
present at migration time. This test seeds NULL rows and re-runs the same
SELECT + grouped UPDATE pattern the migration uses, so the SQL contract is
verified end-to-end.

Uses a sync (psycopg2) engine instead of the async `db_session` fixture to
sidestep the event-loop-mismatch issue that requires Python 3.14+ for the
async-fixture path (see `test_api_request_log_endpoints.py`).

Mirror updates here if the migration body changes.
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from harbor_clerk.file_types import guess_mime_type


@pytest.fixture
def sync_engine(_engine):
    """Sync engine bound to the same test DB. Depends on `_engine` so migrations
    are guaranteed to have run before this test executes."""
    sync_url = os.environ["DATABASE_URL"].replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_backfill_groups_and_updates_null_mime_types(sync_engine):
    """NULL mime_type rows get populated from canonical_filename; non-NULL untouched.

    Seeds a small fixture across 3 distinct mimes (with one mime hit twice so
    the grouping path is exercised), plus a pre-populated row and a
    NULL-canonical-filename row that the migration's SELECT must skip.
    """
    seeded_ids = []
    pre_existing_id = None
    null_fname_id = None

    with sync_engine.begin() as conn:
        for fname in ("contract.pdf", "notice.pdf", "brief.docx", "notes.txt"):
            doc_id = uuid.uuid4()
            seeded_ids.append((doc_id, fname))
            conn.execute(
                text(
                    "INSERT INTO documents (doc_id, title, canonical_filename, status, "
                    "sha256, mime_type, pipeline_status, pipeline_seq) "
                    "VALUES (:doc_id, :title, :fname, 'active', :sha, NULL, 'queued', 0)"
                ),
                {
                    "doc_id": doc_id,
                    "title": fname,
                    "fname": fname,
                    "sha": uuid.uuid4().bytes + uuid.uuid4().bytes[:16],
                },
            )
        # Pre-populated row — must stay untouched.
        pre_existing_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (doc_id, title, canonical_filename, status, "
                "sha256, mime_type, pipeline_status, pipeline_seq) "
                "VALUES (:doc_id, 't', 'already.html', 'active', :sha, 'text/html', 'queued', 0)"
            ),
            {"doc_id": pre_existing_id, "sha": uuid.uuid4().bytes + uuid.uuid4().bytes[:16]},
        )
        # NULL-canonical-filename — the migration's SELECT filter excludes it.
        null_fname_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO documents (doc_id, title, canonical_filename, status, "
                "sha256, mime_type, pipeline_status, pipeline_seq) "
                "VALUES (:doc_id, 't', NULL, 'active', :sha, NULL, 'queued', 0)"
            ),
            {"doc_id": null_fname_id, "sha": uuid.uuid4().bytes + uuid.uuid4().bytes[:16]},
        )

    try:
        # Reproduce the migration body — same query, same grouping, same UPDATE.
        with sync_engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT doc_id, canonical_filename FROM documents "
                    "WHERE mime_type IS NULL AND canonical_filename IS NOT NULL"
                )
            ).fetchall()
            assert len(rows) == 4  # the 4 NULL+filename rows we seeded

            by_mime: dict[str, list] = {}
            for doc_id, fname in rows:
                by_mime.setdefault(guess_mime_type(fname), []).append(doc_id)

            assert set(by_mime.keys()) == {
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "text/plain",
            }
            assert len(by_mime["application/pdf"]) == 2

            for mt, ids in by_mime.items():
                conn.execute(
                    text("UPDATE documents SET mime_type = :mt WHERE doc_id = ANY(:ids) AND mime_type IS NULL"),
                    {"mt": mt, "ids": ids},
                )

        # Verify.
        with sync_engine.begin() as conn:
            final = {
                row.canonical_filename: row.mime_type
                for row in conn.execute(
                    text("SELECT canonical_filename, mime_type FROM documents WHERE canonical_filename IS NOT NULL")
                ).fetchall()
            }
            assert final["contract.pdf"] == "application/pdf"
            assert final["notice.pdf"] == "application/pdf"
            assert final["brief.docx"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            assert final["notes.txt"] == "text/plain"
            # Pre-populated row untouched (the `AND mime_type IS NULL` guard).
            assert final["already.html"] == "text/html"

            # NULL-filename row stays NULL.
            leftover = conn.execute(
                text("SELECT mime_type FROM documents WHERE doc_id = :doc_id"),
                {"doc_id": null_fname_id},
            ).scalar()
            assert leftover is None
    finally:
        # Clean up the rows we inserted so we don't leak across tests
        # (the async db_session fixture's table-cleanup doesn't fire here).
        with sync_engine.begin() as conn:
            ids = [d for d, _ in seeded_ids] + [pre_existing_id, null_fname_id]
            conn.execute(
                text("DELETE FROM documents WHERE doc_id = ANY(:ids)"),
                {"ids": ids},
            )
