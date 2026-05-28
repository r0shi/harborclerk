"""Integration test for migration 0006 — backfill semantics, not Alembic plumbing.

The session-scoped `_engine` fixture runs the migration on an empty DB during
setup, which only proves the migration is syntactically valid. The migration's
real work is the per-extension grouped UPDATE, which only fires when there are
NULL rows present at migration time. This test seeds NULL rows and re-runs the
same SELECT + grouped UPDATE pattern the migration uses, so the SQL contract
is verified end-to-end.

Mirror updates to this test if the migration body changes.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.file_types import guess_mime_type
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import PipelineStatus


@pytest.mark.anyio
async def test_backfill_groups_and_updates_null_mime_types(db_session: AsyncSession):
    """NULL mime_type rows get populated from canonical_filename; non-NULL untouched."""
    # Seed: 4 NULL rows across 3 distinct mimes, 1 row that already has a value
    # (must stay untouched), 1 row with NULL canonical_filename (skipped).
    samples = [
        ("contract.pdf", None),
        ("notice.pdf", None),
        ("brief.docx", None),
        ("notes.txt", None),
        ("already.html", "text/html"),  # pre-populated, must remain
    ]
    docs = []
    for i, (fname, mime) in enumerate(samples):
        d = Document(
            title=fname,
            canonical_filename=fname,
            status="active",
            sha256=bytes([i] * 32),
            mime_type=mime,
            pipeline_status=PipelineStatus.queued,
        )
        db_session.add(d)
        docs.append(d)
    # One additional row with NULL filename — must not be touched (the
    # migration's SELECT filters `canonical_filename IS NOT NULL`).
    null_fname = Document(
        title="t",
        canonical_filename=None,
        status="active",
        sha256=bytes([99] * 32),
        mime_type=None,
        pipeline_status=PipelineStatus.queued,
    )
    db_session.add(null_fname)
    await db_session.flush()

    # Reproduce the migration's body — same query, same grouping, same UPDATE.
    rows = (
        await db_session.execute(
            text(
                "SELECT doc_id, canonical_filename FROM documents "
                "WHERE mime_type IS NULL AND canonical_filename IS NOT NULL"
            )
        )
    ).all()
    assert len(rows) == 4  # 4 NULL+filename rows; the NULL-filename row is excluded

    by_mime: dict[str, list] = {}
    for doc_id, fname in rows:
        by_mime.setdefault(guess_mime_type(fname), []).append(doc_id)

    # 3 distinct mimes across the 4 seeded NULL rows (two .pdf rows merge).
    assert set(by_mime.keys()) == {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    }
    assert len(by_mime["application/pdf"]) == 2

    for mt, ids in by_mime.items():
        await db_session.execute(
            text("UPDATE documents SET mime_type = :mt WHERE doc_id = ANY(:ids) AND mime_type IS NULL"),
            {"mt": mt, "ids": ids},
        )
    await db_session.commit()

    final = {
        row.canonical_filename: row.mime_type
        for row in (
            await db_session.execute(
                text("SELECT canonical_filename, mime_type FROM documents WHERE canonical_filename IS NOT NULL")
            )
        ).all()
    }
    assert final["contract.pdf"] == "application/pdf"
    assert final["notice.pdf"] == "application/pdf"
    assert final["brief.docx"] == ("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert final["notes.txt"] == "text/plain"
    # Pre-populated row must remain on its original mime; the migration's
    # `AND mime_type IS NULL` guard is what protects it.
    assert final["already.html"] == "text/html"

    # And the NULL-filename row stays NULL.
    leftover = (
        await db_session.execute(text("SELECT mime_type FROM documents WHERE canonical_filename IS NULL"))
    ).scalar()
    assert leftover is None
