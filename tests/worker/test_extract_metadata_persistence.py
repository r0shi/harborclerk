"""End-to-end: the extract stage runs the extractor framework and persists
results to Document.doc_metadata. Uses a sidecar JSON file next to the source
file — no Tika call needed for plain-text files so the test stays hermetic."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder


async def _watch(db_session, doc: Document, source: Path) -> None:
    """Map ``doc`` to a watched file, as the watcher does for a document it reads in place."""
    folder = WatchedFolder(path=str(source.parent), display_name=source.parent.name)
    db_session.add(folder)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path=source.name,
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )


@pytest.mark.asyncio
async def test_extract_stage_writes_sidecar_metadata_to_doc(db_session, tmp_path: Path):
    """run_extract on a .txt file with a companion sidecar .json populates
    doc.doc_metadata['sidecar'] and doc.doc_metadata['_source_provenance']."""
    # Set up source file + sidecar
    source = tmp_path / "0001_invoice.txt"
    source.write_text("Invoice body text")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text(json.dumps({"vendor": "Acme", "total_usd": 100}))

    doc = Document(
        title="0001_invoice",
        canonical_filename="0001_invoice.txt",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="text/plain",
        source_path=str(source),
    )
    db_session.add(doc)
    await db_session.flush()
    await _watch(db_session, doc, source)
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    # run_extract is sync — call directly (same pattern as test_pipeline.py).
    from harbor_clerk.worker.stages.extract import run_extract

    run_extract(doc_id)

    # Re-fetch via a fresh sync session (run_extract commits its own session).
    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        metadata = refreshed.doc_metadata
    finally:
        sync_session.close()

    assert "sidecar" in metadata, f"expected 'sidecar' key in doc_metadata, got: {metadata}"
    assert metadata["sidecar"] == {"vendor": "Acme", "total_usd": 100}
    assert "_source_provenance" in metadata
    assert "sidecar" in metadata["_source_provenance"]


async def _extract_with_stored_original(db_session, tmp_path: Path, *, watched: bool) -> tuple[dict | None, int | None]:
    """Extract a document whose original is in storage and whose source_path
    names a different file with a sidecar beside it. Returns (metadata, chars)."""
    import io
    import uuid

    from harbor_clerk.storage import get_storage

    body = b"Stored body text"
    labelled = tmp_path / "0002_doc.txt"
    labelled.write_text("a different file at the source_path")
    (tmp_path / "0002_doc.json").write_text(json.dumps({"vendor": "Beside"}))

    bucket = "originals"
    key = f"originals/docs/{uuid.uuid4()}/0002_doc.txt"
    get_storage().put_object(bucket, key, io.BytesIO(body), len(body), "text/plain")

    doc = Document(
        title="0002_doc",
        canonical_filename="0002_doc.txt",
        status="active",
        sha256=hashlib.sha256(body).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="text/plain",
        source_path=str(labelled),
        original_bucket=bucket,
        original_object_key=key,
    )
    db_session.add(doc)
    await db_session.flush()
    if watched:
        await _watch(db_session, doc, labelled)
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    from harbor_clerk.worker.stages.extract import run_extract

    try:
        run_extract(doc_id)
    finally:
        get_storage().remove_object(bucket, key)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        return refreshed.doc_metadata, refreshed.extracted_chars
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_extract_stage_reads_no_sidecar_beside_an_uploads_source_path(db_session, tmp_path: Path):
    """An upload's source_path is a client-supplied label: the extract stage reads
    its bytes from storage and attaches no file found beside that path."""
    metadata, extracted_chars = await _extract_with_stored_original(db_session, tmp_path, watched=False)

    assert extracted_chars == len(b"Stored body text"), "the body must come from storage"
    assert "sidecar" not in (metadata or {}), f"no sidecar may be read for an upload, got: {metadata}"


@pytest.mark.asyncio
async def test_extract_stage_reads_the_sidecar_of_a_watched_doc_with_a_stored_original(db_session, tmp_path: Path):
    """A watched document that also carries an object key (a confirmed new
    version) is still read in place for its sidecar."""
    metadata, _ = await _extract_with_stored_original(db_session, tmp_path, watched=True)

    assert (metadata or {}).get("sidecar") == {"vendor": "Beside"}, f"expected the sidecar, got: {metadata}"


@pytest.mark.asyncio
async def test_extract_stage_clears_stale_metadata_when_extractors_return_empty(db_session, tmp_path: Path):
    """Re-ingesting a doc whose sidecar was deleted should drop the stale
    sidecar metadata, not keep it sticky. Same applies if Tika is unavailable
    and no other extractor produces results — doc_metadata should be {},
    not the pre-extract value."""
    from unittest.mock import patch

    from harbor_clerk.worker.stages import extract

    source = tmp_path / "0001_invoice.txt"
    source.write_text("Body without any sidecar.")
    # NOTE: deliberately NO sidecar file created.

    doc = Document(
        title="0001_invoice",
        canonical_filename="0001_invoice.txt",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="text/plain",
        source_path=str(source),
        # Pre-existing stale metadata that should be cleared
        doc_metadata={
            "sidecar": {"vendor": "OldVendor"},
            "_source_provenance": {"sidecar": "2026-01-01T00:00:00+00:00"},
        },
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    with patch.object(extract, "_extract_via_tika", return_value=[(1, "Body without any sidecar.")]):
        from harbor_clerk.worker.stages.extract import run_extract

        run_extract(doc_id)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        metadata = refreshed.doc_metadata
    finally:
        sync_session.close()

    # Stale "OldVendor" should be gone, since the sidecar file doesn't exist
    # and Tika has no body-format-specific metadata for plain text.
    assert "sidecar" not in metadata, f"stale sidecar key should be cleared, got: {metadata}"
    # The whole metadata dict should be empty (or at most contain whatever
    # Tika /meta returned, which for text/plain in this hermetic env is None).
    assert metadata == {}


@pytest.mark.asyncio
async def test_extract_stage_backfills_email_columns_on_legacy_eml(db_session, tmp_path: Path):
    """Backfill path: an .eml doc that was ingested before the watcher fix
    (email_message_id is NULL) gets its email_* columns populated when extract
    runs. This is how `kb_reprocess` lights up the existing 10k+ watched-folder
    .eml docs that pre-date the watcher-side fix in this PR — operators don't
    need a separate backfill script.

    Tika is mocked so the test is hermetic (the test environment may not have
    a Tika container running)."""
    from unittest.mock import patch

    from harbor_clerk.worker.stages import extract

    eml_bytes = (
        b'From: "Alice" <alice@example.com>\r\n'
        b"To: bob@example.com\r\n"
        b"Subject: Legacy email backfill test\r\n"
        b"Message-ID: <legacy-backfill-001@example.com>\r\n"
        b"Date: Wed, 28 May 2026 12:00:00 +0000\r\n"
        b"\r\n"
        b"Body content.\r\n"
    )
    source = tmp_path / "legacy.eml"
    source.write_bytes(eml_bytes)

    doc = Document(
        title="legacy",  # filename stem — pre-this-PR behavior
        canonical_filename="legacy.eml",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="message/rfc822",
        source_path=str(source),
        # NOTE: all email_* columns deliberately NULL — simulating pre-PR ingest.
        email_message_id=None,
        email_subject=None,
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    # Mock Tika — real extraction of an .eml would return some pages; we
    # don't need realism here, just SOMETHING for the preamble-injection
    # block to consume so we can verify it fires too.
    with patch.object(extract, "_extract_via_tika", return_value=[(1, "Body content.")]):
        from harbor_clerk.worker.stages.extract import run_extract

        run_extract(doc_id)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
    finally:
        sync_session.close()

    # email_* columns populated from the raw bytes via parse_eml.
    assert refreshed.email_message_id == "<legacy-backfill-001@example.com>"
    assert refreshed.email_subject == "Legacy email backfill test"
    assert refreshed.email_from_address == "alice@example.com"
    assert refreshed.email_to_addresses == ["bob@example.com"]
    assert refreshed.email_date_sent is not None
    # Title adopts parsed.subject only when the previous title looked like
    # the filename stem (guard against clobbering an operator edit).
    assert refreshed.title == "Legacy email backfill test"
    # created_at slides to the email's send date — matches IMAP-path + new
    # watcher-path semantics so all email rows sort uniformly.
    assert refreshed.created_at == refreshed.email_date_sent


@pytest.mark.asyncio
async def test_extract_stage_does_not_clobber_existing_email_columns(db_session, tmp_path: Path):
    """If email_message_id is already populated (e.g. IMAP-ingested doc, or
    watcher fix already ran), the backfill block must be a no-op — we don't
    want to overwrite IMAP-side data (e.g. email_label_path) with the
    parse_eml-derived view, and re-parsing on every extract would be wasteful."""
    from unittest.mock import patch

    from harbor_clerk.worker.stages import extract

    eml_bytes = (
        b"From: alice@example.com\r\nSubject: Already populated\r\nMessage-ID: <new-id@example.com>\r\n\r\nBody.\r\n"
    )
    source = tmp_path / "imap.eml"
    source.write_bytes(eml_bytes)

    doc = Document(
        title="IMAP set this",
        canonical_filename="imap.eml",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="message/rfc822",
        source_path=str(source),
        email_message_id="<existing-imap-id@example.com>",  # ALREADY SET
        email_subject="IMAP set this subject",
        email_label_path="INBOX/Work",  # IMAP-only field
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    with patch.object(extract, "_extract_via_tika", return_value=[(1, "Body.")]):
        from harbor_clerk.worker.stages.extract import run_extract

        run_extract(doc_id)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
    finally:
        sync_session.close()

    # No clobbering — pre-existing values preserved.
    assert refreshed.email_message_id == "<existing-imap-id@example.com>"
    assert refreshed.email_subject == "IMAP set this subject"
    assert refreshed.email_label_path == "INBOX/Work"
    assert refreshed.title == "IMAP set this"
