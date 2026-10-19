"""End-to-end: the extract stage runs the extractor framework and persists
results to Document.doc_metadata. Uses a sidecar JSON file next to the source
file — no Tika call needed for plain-text files so the test stays hermetic."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder
from tests.conftest import watch_document


async def _watch(
    db_session,
    doc: Document,
    source: Path,
    *,
    enabled: bool = True,
    status: WatchedFileStatus = WatchedFileStatus.active,
) -> None:
    """Map ``doc`` to a watched file, as the watcher does for a document it reads in place."""
    folder = WatchedFolder(path=str(source.parent), display_name=source.parent.name, enabled=enabled)
    db_session.add(folder)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=folder.folder_id,
            relative_path=source.name,
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=status,
        )
    )


async def _extract_watched_doc(
    db_session,
    tmp_path: Path,
    *,
    enabled: bool = True,
    status: WatchedFileStatus = WatchedFileStatus.active,
    name: str = "0003_doc.txt",
    disk: bytes = b"Edited on disk body text",
    stored: bytes | None = None,
    stored_name: str | None = None,
    mime_type: str = "text/plain",
    delete_file: bool = False,
    raises: type[BaseException] | None = None,
    watch: bool = True,
) -> SimpleNamespace:
    """Extract a watched document (its file and a sidecar on disk), optionally also
    carrying a stored original under ``stored_name`` whose bytes, name and
    ``mime_type`` describe an older upload. ``delete_file`` removes the file
    (keeping the sidecar) before the extract. Returns metadata, chars, text, mime."""
    import io
    import uuid

    from harbor_clerk.models import DocumentPage
    from harbor_clerk.storage import get_storage

    source = tmp_path / name
    source.write_bytes(disk)
    source.with_suffix(".json").write_text(json.dumps({"vendor": "Acme"}))
    key = None
    if stored is not None:
        key = f"originals/docs/{uuid.uuid4()}/{stored_name or name}"
        get_storage().put_object("originals", key, io.BytesIO(stored), len(stored), mime_type)
    doc = Document(
        title=source.stem,
        canonical_filename=name,
        status="active",
        sha256=hashlib.sha256(disk).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type=mime_type,
        source_path=str(source),
        original_bucket="originals" if key else None,
        original_object_key=key,
    )
    db_session.add(doc)
    await db_session.flush()
    if watch:
        await _watch(db_session, doc, source, enabled=enabled, status=status)
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id
    if delete_file:
        source.unlink()

    from harbor_clerk.worker.stages.extract import run_extract

    try:
        if raises is None:
            run_extract(doc_id)
        else:
            with pytest.raises(raises):
                run_extract(doc_id)
    finally:
        if key:
            get_storage().remove_object("originals", key)

    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        pages = (
            sync_session.execute(select(DocumentPage.page_text).where(DocumentPage.doc_id == doc_id)).scalars().all()
        )
        return SimpleNamespace(
            metadata=refreshed.doc_metadata or {},
            chars=refreshed.extracted_chars,
            text=" ".join(pages),
            mime=refreshed.mime_type,
        )
    finally:
        sync_session.close()


@pytest.mark.asyncio
async def test_extract_stage_reads_the_sidecar_of_a_paused_folders_document(db_session, tmp_path: Path):
    """A disabled folder's documents are still read in place, sidecar included."""
    out = await _extract_watched_doc(db_session, tmp_path, enabled=False)
    assert out.metadata.get("sidecar") == {"vendor": "Acme"}, f"expected the sidecar, got: {out.metadata}"


@pytest.mark.asyncio
async def test_extract_stage_never_reads_the_stored_original_of_a_removed_watched_file(db_session, tmp_path: Path):
    """A watched document whose watched file is removed is missing: the stage fails
    rather than read its stored original, which may be an older upload."""
    from harbor_clerk.watched_source import WatchedFileMissing

    out = await _extract_watched_doc(
        db_session, tmp_path, status=WatchedFileStatus.removed, stored=b"Stored body text", raises=WatchedFileMissing
    )
    assert "Stored body text" not in out.text, out.text
    assert out.chars is None
    assert "sidecar" not in out.metadata, f"no sidecar expected, got: {out.metadata}"


@pytest.mark.asyncio
async def test_extract_stage_never_reads_an_unverified_source_path(db_session, tmp_path: Path):
    """A document that is not watched and has no stored original has nothing to read:
    the stage fails, rather than reading the file at source_path."""
    from harbor_clerk.watched_source import NoReadableSource

    out = await _extract_watched_doc(db_session, tmp_path, watch=False, raises=NoReadableSource)
    assert out.chars is None
    assert out.text == ""


@pytest.mark.asyncio
async def test_extract_stage_reads_a_watched_doc_from_disk_despite_a_stale_stored_original(db_session, tmp_path: Path):
    """A watched document that also carries an older stored original (#859) is
    extracted from its file, so a disk edit reaches the index with its sidecar."""
    out = await _extract_watched_doc(db_session, tmp_path, stored=b"Stale stored body")
    assert "Edited on disk" in out.text and "Stale" not in out.text, out.text
    assert out.chars == len("Edited on disk body text")
    assert out.metadata.get("sidecar") == {"vendor": "Acme"}


@pytest.mark.asyncio
async def test_extract_stage_fails_when_the_watched_file_is_missing(db_session, tmp_path: Path):
    """With its file missing (mid atomic save, or gone), a watched document fails the
    stage instead of being read from its stored original, which may be an older upload."""
    from harbor_clerk.watched_source import WatchedFileMissing

    out = await _extract_watched_doc(
        db_session, tmp_path, stored=b"Stored body text", delete_file=True, raises=WatchedFileMissing
    )
    assert "Stored body text" not in out.text, out.text
    assert out.chars is None
    assert "sidecar" not in out.metadata, f"no sidecar expected, got: {out.metadata}"


@pytest.mark.asyncio
async def test_extract_stage_routes_a_watched_pdf_by_its_file_not_a_stale_markdown_original(
    db_session, tmp_path: Path, monkeypatch
):
    """The file is report.pdf; the stale stored original was report.md. The PDF goes
    to the PDF extractor, not the markdown path, and its mime is corrected."""
    from harbor_clerk.worker.stages import extract

    calls: list[str] = []

    def fake_tika(data, mime, **kwargs):
        calls.append(mime)
        return [(1, "Text of the real PDF")]

    def no_markdown(data):
        raise AssertionError("PDF bytes must not take the markdown path")

    monkeypatch.setattr(extract, "_extract_via_tika", fake_tika)
    monkeypatch.setattr(extract, "extract_markdown", no_markdown)
    out = await _extract_watched_doc(
        db_session,
        tmp_path,
        name="report.pdf",
        disk=b"%PDF-1.4\n%fake pdf body\n",
        stored=b"# Old markdown upload\n",
        stored_name="report.md",
        mime_type="text/markdown",
    )
    assert calls and calls[0] == "application/pdf", calls
    assert out.text == "Text of the real PDF"
    assert out.mime == "application/pdf"


@pytest.mark.asyncio
async def test_extract_stage_reads_a_watched_text_file_despite_a_stale_pdf_original(
    db_session, tmp_path: Path, monkeypatch
):
    """The file is notes.txt; the stale stored original was a PDF. It is decoded as
    text, without Tika."""
    from harbor_clerk.worker.stages import extract

    def no_tika(data, mime, **kwargs):
        raise AssertionError(f"a text file must not go to Tika (as {mime})")

    monkeypatch.setattr(extract, "_extract_via_tika", no_tika)
    out = await _extract_watched_doc(
        db_session,
        tmp_path,
        name="notes.txt",
        disk=b"Plain notes on disk",
        stored=b"%PDF-1.4 old upload",
        stored_name="notes.pdf",
        mime_type="application/pdf",
    )
    assert out.text == "Plain notes on disk"
    assert out.mime == "text/plain"


@pytest.mark.asyncio
async def test_extract_stage_fails_when_the_watched_file_lookup_fails(db_session, tmp_path: Path, monkeypatch):
    """A failed watched-file lookup fails the stage (to be retried); it never falls
    back to the stored original, which would index the older upload's bytes."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    import harbor_clerk.watched_source as watched_source

    monkeypatch.setattr(watched_source, "watched_paths_query", lambda doc_id: text("SELECT * FROM no_such_table_603"))
    out = await _extract_watched_doc(db_session, tmp_path, stored=b"Stale stored body", raises=SQLAlchemyError)
    assert "Stale" not in out.text, out.text
    assert out.chars is None, "nothing may be committed from the stored original"
    assert out.mime == "text/plain"


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


async def _extract_with_stored_original(
    db_session, tmp_path: Path, *, watched_file: Path | None, raises: type[BaseException] | None = None
) -> tuple[dict | None, int | None]:
    """Extract a document whose original is in storage and whose source_path
    names a different file with a sidecar beside it, optionally mapped to the
    watched file ``watched_file``. Returns (metadata, chars)."""
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
    if watched_file is not None:
        await _watch(db_session, doc, watched_file)
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    from harbor_clerk.worker.stages.extract import run_extract

    try:
        if raises is None:
            run_extract(doc_id)
        else:
            with pytest.raises(raises):
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
    metadata, extracted_chars = await _extract_with_stored_original(db_session, tmp_path, watched_file=None)

    assert extracted_chars == len(b"Stored body text"), "the body must come from storage"
    assert "sidecar" not in (metadata or {}), f"no sidecar may be read for an upload, got: {metadata}"


@pytest.mark.asyncio
async def test_extract_stage_reads_no_sidecar_when_source_path_is_not_the_watched_path(db_session, tmp_path: Path):
    """A document mapped to a watched file but whose source_path names some other
    file (an alias not yet repaired, or a label) is watched but not read in place:
    the stage fails as missing. Neither its stored original nor any sidecar is read."""
    from harbor_clerk.watched_source import WatchedFileMissing

    folder = tmp_path / "folder"
    folder.mkdir()
    watched = folder / "real.txt"
    watched.write_text("the watched file")
    (folder / "real.json").write_text(json.dumps({"vendor": "WatchedSidecar"}))

    metadata, chars = await _extract_with_stored_original(
        db_session, tmp_path, watched_file=watched, raises=WatchedFileMissing
    )

    assert chars is None, "nothing may be read from the stored original"
    assert "sidecar" not in (metadata or {}), f"no sidecar may be read, got: {metadata}"


@pytest.mark.asyncio
async def test_extract_stage_clears_stale_metadata_when_extractors_return_empty(db_session, tmp_path: Path):
    """Re-ingesting a watched doc whose sidecar was deleted should drop the stale
    sidecar metadata, not keep it sticky. Same applies if Tika is unavailable
    and no other extractor produces results — doc_metadata should be {},
    not the pre-extract value. The first extract, with the sidecar present,
    shows the document is read in place."""
    from unittest.mock import patch

    from harbor_clerk.worker.stages import extract

    source = tmp_path / "0001_invoice.txt"
    source.write_text("Body without any sidecar.")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text(json.dumps({"vendor": "Acme"}))

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
    await _watch(db_session, doc, source)
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    with patch.object(extract, "_extract_via_tika", return_value=[(1, "Body without any sidecar.")]):
        from harbor_clerk.worker.stages.extract import run_extract

        run_extract(doc_id)
        sync_session = get_sync_session()
        try:
            first = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one().doc_metadata
        finally:
            sync_session.close()
        assert first.get("sidecar") == {"vendor": "Acme"}, f"the watched doc must read its sidecar first: {first}"

        sidecar.unlink()
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
    await watch_document(db_session, doc, source)
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
    await watch_document(db_session, doc, source)
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


@pytest.mark.asyncio
async def test_mark_stage_done_commits_when_the_display_name_lookup_fails(db_session, tmp_path: Path, monkeypatch):
    """The display-name lookup runs in a savepoint: its failure costs the name, not
    the job and document writes that mark_stage_done commits."""
    import uuid

    from sqlalchemy import text

    import harbor_clerk.watched_source as watched_source
    from harbor_clerk.worker.pipeline import mark_stage_done

    source = tmp_path / "0004_doc.txt"
    source.write_text("body")
    doc = Document(
        title="0004_doc",
        canonical_filename="0004_doc.txt",
        status="active",
        sha256=hashlib.sha256(b"body 0004").digest(),
        pipeline_status=PipelineStatus.finalizing,
        mime_type="text/plain",
        source_path=str(source),
        original_bucket="originals",
        original_object_key=f"originals/docs/{uuid.uuid4()}/0004_doc.txt",
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.finalize, status=JobStatus.running))
    await db_session.commit()
    doc_id = doc.doc_id

    monkeypatch.setattr(watched_source, "watched_paths_query", lambda doc_id: text("SELECT * FROM no_such_table_603"))
    mark_stage_done(doc_id, JobStage.finalize)

    sync_session = get_sync_session()
    try:
        job = sync_session.execute(
            select(IngestionJob).where(IngestionJob.doc_id == doc_id, IngestionJob.stage == JobStage.finalize)
        ).scalar_one()
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        assert job.status == JobStatus.done
        assert refreshed.pipeline_status == PipelineStatus.ready
    finally:
        sync_session.close()
