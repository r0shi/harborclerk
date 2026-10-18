"""watched_source.display_filename: the name job events and logs show for a document."""

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFileStatus, WatchedFolder


async def _doc(db_session, tmp_path: Path, *, watched: bool) -> uuid.UUID:
    """A document whose source_path is tmp_path/report.txt and whose stored original is old-upload.pdf."""
    source = tmp_path / "report.txt"
    source.write_text("on disk")
    doc = Document(
        title="report",
        status="active",
        sha256=hashlib.sha256(str(uuid.uuid4()).encode()).digest(),
        pipeline_status=PipelineStatus.ready,
        source_path=str(source),
        original_bucket="originals",
        original_object_key=f"originals/docs/{uuid.uuid4()}/old-upload.pdf",
    )
    db_session.add(doc)
    await db_session.flush()
    if watched:
        folder = WatchedFolder(path=str(tmp_path), display_name=tmp_path.name)
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
    await db_session.commit()
    return doc.doc_id


def _display_filename(doc_id: uuid.UUID) -> str:
    from harbor_clerk.watched_source import display_filename

    session = get_sync_session()
    try:
        doc = session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        return display_filename(session, doc)
    finally:
        session.close()


@pytest.mark.asyncio
async def test_a_watched_doc_with_a_stale_original_shows_its_files_name(db_session, tmp_path: Path):
    doc_id = await _doc(db_session, tmp_path, watched=True)
    assert _display_filename(doc_id) == "report.txt"


@pytest.mark.asyncio
async def test_an_upload_shows_its_stored_originals_name(db_session, tmp_path: Path):
    doc_id = await _doc(db_session, tmp_path, watched=False)
    assert _display_filename(doc_id) == "old-upload.pdf"


@pytest.mark.asyncio
async def test_a_failed_lookup_falls_back_to_the_stored_name(db_session, tmp_path: Path, monkeypatch):
    """Display only: a failed lookup is not an error."""
    from sqlalchemy import text

    import harbor_clerk.watched_source as watched_source

    doc_id = await _doc(db_session, tmp_path, watched=True)
    monkeypatch.setattr(watched_source, "watched_paths_query", lambda doc_id: text("SELECT * FROM no_such_table_603"))
    assert _display_filename(doc_id) == "old-upload.pdf"


@pytest.mark.asyncio
async def test_worker_lookup_filename_shows_the_watched_files_name(db_session, tmp_path: Path):
    from harbor_clerk.worker.entry import _lookup_filename

    doc_id = await _doc(db_session, tmp_path, watched=True)
    assert _lookup_filename(doc_id) == "report.txt"


@pytest.mark.parametrize(
    ("exc", "status", "expected"),
    [
        ("missing", "error", True),
        ("missing-empty", "error", True),
        ("no-source", "error", True),
        ("other", "error", False),
        ("missing", "ready", False),
    ],
)
def test_failed_for_a_missing_source_reads_the_error_the_worker_records(exc, status, expected):
    """The predicate matches describe_error's rendering of the two missing-source errors only."""
    from types import SimpleNamespace

    from harbor_clerk.error_text import describe_error
    from harbor_clerk.watched_source import NoReadableSource, WatchedFileMissing, failed_for_a_missing_source

    raised = {
        "missing": WatchedFileMissing("the watched file is missing"),
        "missing-empty": WatchedFileMissing(),
        "no-source": NoReadableSource("no stored original"),
        "other": RuntimeError("WatchedFileMissing appears only in the message"),
    }[exc]
    doc = SimpleNamespace(pipeline_status=PipelineStatus(status), error=describe_error(raised))
    assert failed_for_a_missing_source(doc) is expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("RuntimeError: No source for doc 0b5e: source_path and original_object_key both empty", True),
        ("RuntimeError: No source for doc 0b5e", True),
        ("RuntimeError: No sourcefile here", False),
        ("ValueError: No source for doc 0b5e", False),
        ("RuntimeError: Tika returned 422", False),
    ],
)
def test_failed_for_a_missing_source_matches_the_pre_upgrade_error_exactly(error, expected):
    """Documents that errored before the upgrade with the stages' old missing-source
    error recover too; nothing broader matches."""
    from types import SimpleNamespace

    from harbor_clerk.watched_source import failed_for_a_missing_source

    doc = SimpleNamespace(pipeline_status=PipelineStatus.error, error=error)
    assert failed_for_a_missing_source(doc) is expected


def test_is_file_exact_case(tmp_path):
    """A regular file (or a symlink to one) under exactly that name; not a dangling
    symlink, not a directory, not another case."""
    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "note.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "note.txt")
    (tmp_path / "dangling.txt").symlink_to(tmp_path / "nowhere.txt")
    (tmp_path / "dir.txt").mkdir()
    assert is_file_exact_case(str(tmp_path / "note.txt"))
    assert is_file_exact_case(str(tmp_path / "link.txt"))
    assert not is_file_exact_case(str(tmp_path / "gone.txt"))
    assert not is_file_exact_case(str(tmp_path / "dangling.txt"))
    assert not is_file_exact_case(str(tmp_path / "dir.txt"))


def test_is_file_exact_case_refuses_a_case_only_match(tmp_path, monkeypatch):
    """On a case-insensitive filesystem isfile answers yes for Note.txt when note.txt
    exists; the listing refuses it. Simulated here so it holds on case-sensitive CI."""
    import os

    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "note.txt").write_text("x")
    real_isfile = os.path.isfile
    monkeypatch.setattr(os.path, "isfile", lambda p: str(p).lower() == str(tmp_path / "note.txt") or real_isfile(p))
    assert not is_file_exact_case(str(tmp_path / "Note.txt"))
    assert is_file_exact_case(str(tmp_path / "note.txt"))


def test_is_file_exact_case_lists_the_parent_only_for_a_file(tmp_path, monkeypatch):
    """Listing a large directory on every delete event is O(entries): isfile goes first."""
    import os

    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "note.txt").write_text("x")
    (tmp_path / "dir.txt").mkdir()
    calls: list[str] = []
    real_listdir = os.listdir

    def counting_listdir(path="."):
        calls.append(str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", counting_listdir)
    assert not is_file_exact_case(str(tmp_path / "gone.txt"))
    assert not is_file_exact_case(str(tmp_path / "dir.txt"))
    assert calls == []
    assert is_file_exact_case(str(tmp_path / "note.txt"))
    assert calls == [str(tmp_path)]


async def _watched_doc_with_stored_original(db_session, tmp_path):
    """A watched document at tmp_path/folder/sub/doc.txt with a stored original. Returns (doc_id, file path)."""
    folder = tmp_path / "folder"
    (folder / "sub").mkdir(parents=True)
    source = folder / "sub" / "doc.txt"
    source.write_text("on disk")
    doc = Document(
        title="doc",
        status="active",
        sha256=hashlib.sha256(b"on disk").digest(),
        pipeline_status=PipelineStatus.ready,
        source_path=str(source),
        original_bucket="originals",
        original_object_key=f"originals/docs/{uuid.uuid4()}/doc.txt",
    )
    db_session.add(doc)
    await db_session.flush()
    wf_folder = WatchedFolder(path=str(folder), display_name="folder")
    db_session.add(wf_folder)
    await db_session.flush()
    db_session.add(
        WatchedFile(
            folder_id=wf_folder.folder_id,
            relative_path="sub/doc.txt",
            bookmark_data=b"",
            sha256=doc.sha256,
            doc_id=doc.doc_id,
            status=WatchedFileStatus.active,
        )
    )
    await db_session.commit()
    return doc.doc_id, source


@pytest.mark.parametrize("breakage", ["not-a-directory", "permission"])
async def test_read_document_bytes_raises_missing_without_a_path_for_any_os_error(
    db_session, tmp_path, breakage, caplog
):
    """A path component replaced by a file, or no permission, is "cannot be read in
    place": WatchedFileMissing, whose message names the errno and no path (it lands in
    doc.error). The read error is logged at WARNING with the errno, the path in the log only."""
    import logging
    import os
    import shutil

    from harbor_clerk.watched_source import WatchedFileMissing, read_document_bytes

    if breakage == "permission" and os.geteuid() == 0:
        pytest.skip("root reads a file whatever its mode")
    doc_id, source = await _watched_doc_with_stored_original(db_session, tmp_path)
    if breakage == "not-a-directory":
        shutil.rmtree(source.parent)
        source.parent.write_text("a file where the directory was")
    else:
        source.chmod(0)
    session = get_sync_session()
    try:
        doc = session.get(Document, doc_id)
        with (
            caplog.at_level(logging.INFO, logger="harbor_clerk.watched_source"),
            pytest.raises(WatchedFileMissing) as raised,
        ):
            read_document_bytes(session, doc)
    finally:
        session.close()
        if breakage == "permission":
            source.chmod(0o644)
    errname = "ENOTDIR" if breakage == "not-a-directory" else "EACCES"
    assert errname in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and errname in r.getMessage()]
    assert warnings, [r.getMessage() for r in caplog.records]
    assert str(doc_id) in warnings[0].getMessage()
