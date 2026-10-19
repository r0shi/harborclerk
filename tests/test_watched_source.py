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
    """Listing a large directory on every delete event is O(entries): isfile goes first,
    and a file's spelling is read without a listing where it can be (F_GETPATH on
    macOS; on a case-sensitive volume no other spelling resolves)."""
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
    assert calls == []


def _case_insensitive_isfile(monkeypatch, *existing):
    """Simulate APFS's case-insensitive resolution for ``existing`` so the test holds on CI's ext4."""
    import os

    real_isfile = os.path.isfile
    wanted = {str(p).lower() for p in existing}
    monkeypatch.setattr(os.path, "isfile", lambda p: str(p).lower() in wanted or real_isfile(p))


def test_is_file_exact_case_with_a_root_checks_every_component(tmp_path, monkeypatch):
    """After `mv Invoices invoices`, isfile still answers for Invoices/x.txt on APFS;
    with the watched root given, the directory's spelling is checked too."""
    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "invoices").mkdir()
    (tmp_path / "invoices" / "x.txt").write_text("x")
    _case_insensitive_isfile(monkeypatch, tmp_path / "invoices" / "x.txt")
    assert not is_file_exact_case(str(tmp_path / "Invoices" / "x.txt"), str(tmp_path))
    assert is_file_exact_case(str(tmp_path / "invoices" / "x.txt"), str(tmp_path))
    assert is_file_exact_case(str(tmp_path / "Invoices" / "x.txt")), "without a root, the leaf only"


def test_is_file_exact_case_counts_a_file_under_an_unlistable_directory_present(tmp_path, monkeypatch):
    """Mode 0311: the file resolves but its directory cannot be listed. Guessing it
    gone would retire a live document."""
    import os

    import harbor_clerk.watched_source as watched_source
    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "doc.txt").write_text("x")
    real_listdir = os.listdir

    def refusing_listdir(path="."):
        if os.path.basename(str(path)) == "locked":
            raise PermissionError(13, "Permission denied", path)
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", refusing_listdir)
    # Where the spelling cannot be read directly, the listing decides.
    monkeypatch.setattr(watched_source, "_spelled_as_given", lambda *args: False)
    assert is_file_exact_case(str(tmp_path / "locked" / "doc.txt"))
    assert is_file_exact_case(str(tmp_path / "locked" / "doc.txt"), str(tmp_path))


def _counting_listdir(monkeypatch) -> list[str]:
    import os

    calls: list[str] = []
    real_listdir = os.listdir
    monkeypatch.setattr(os, "listdir", lambda path=".": calls.append(str(path)) or real_listdir(path))
    return calls


def test_checks_in_a_large_directory_list_nothing(tmp_path, monkeypatch):
    """Every move checks its destination: listing per file made `mv proj proj2023`
    with 8,000 files cost 17 s under the folder lock. Nothing is cached instead:
    a FAT or exFAT volume root's stat never changes, so a cached listing could be
    served forever."""
    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "big").mkdir()
    for i in range(500):
        (tmp_path / "big" / f"{i:03d}.txt").write_text("x")
    calls = _counting_listdir(monkeypatch)
    assert all(is_file_exact_case(str(tmp_path / "big" / f"{i:03d}.txt"), str(tmp_path)) for i in range(500))
    assert calls == []


def test_a_spelling_read_another_way_is_settled_by_the_listing(tmp_path, monkeypatch):
    """A symlinked directory reports its target's path: not proof the spelling is
    wrong, so the listing decides (and the file is present)."""
    import os

    from harbor_clerk.watched_source import is_file_exact_case

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "x.txt").write_text("x")
    os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
    assert is_file_exact_case(str(tmp_path / "link" / "x.txt"), str(tmp_path))


def test_the_fast_path_never_confirms_another_case_on_a_case_insensitive_volume(tmp_path, monkeypatch):
    """Without F_GETPATH (Linux), a component is exact only when no other spelling
    resolves to the same file; on a volume that ignores case one always does, and
    the listing decides."""
    import harbor_clerk.watched_source as watched_source
    from tests.watcher._fs import volume_is_case_insensitive

    (tmp_path / "Invoices").mkdir()
    (tmp_path / "Invoices" / "doc.txt").write_text("x")
    monkeypatch.setattr(watched_source, "_on_disk_path", lambda path: None)
    insensitive = volume_is_case_insensitive(tmp_path)
    exact = watched_source._no_other_spelling_resolves(str(tmp_path), ["Invoices", "doc.txt"])
    assert exact is not insensitive
    assert not watched_source._no_other_spelling_resolves(str(tmp_path), ["invoices", "doc.txt"])
    assert watched_source.is_file_exact_case(str(tmp_path / "Invoices" / "doc.txt"), str(tmp_path))
    assert not watched_source.is_file_exact_case(str(tmp_path / "invoices" / "doc.txt"), str(tmp_path))


def test_repaired_source_path_requires_every_component_spelled_as_on_disk(tmp_path, monkeypatch):
    """A watched file whose directory was renamed in case only is not present under
    its recorded path: the label is not repaired to a path that is not the file's."""
    from harbor_clerk.watched_source import repaired_source_path

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")
    _case_insensitive_isfile(monkeypatch, tmp_path / "sub" / "a.txt")
    assert repaired_source_path("label", str(tmp_path), "Sub/a.txt") is None
    assert repaired_source_path("label", str(tmp_path), "sub/a.txt") == str(tmp_path / "sub" / "a.txt")


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
