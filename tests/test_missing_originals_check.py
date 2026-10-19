"""The API startup check for documents whose stored originals are missing (#888).

A normal upgrade (`git pull && docker compose up -d --build`) starts the
filesystem-backed stack on an empty `originals` volume. Compose always sets
STORAGE_BACKEND, so get_storage()'s unset-variable warning never fires; this
ERROR is what points the operator at the MinIO migration. It samples the oldest
documents with a stored original rather than asking whether the bucket is
empty: on an IMAP install the watcher stores new mail before `app` starts, and
any new upload would have silenced an emptiness check for good.
"""

import io
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import harbor_clerk.storage as storage_mod
from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.storage import MISSING_ORIGINALS_SAMPLE, FilesystemBackend, log_missing_originals

BUCKET = "originals"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def fs(tmp_path, monkeypatch):
    with patch("harbor_clerk.storage.get_settings") as settings:
        settings.return_value.storage_path = str(tmp_path)
        backend = FilesystemBackend()
    monkeypatch.setattr(storage_mod, "get_storage", lambda: backend)
    return backend


async def _add(session, n: int, *, key: str | None, status: str = "active") -> None:
    session.add(
        Document(
            title=f"Missing originals check {n}",
            canonical_filename=f"doc{n}.pdf",
            status=status,
            sha256=bytes([n + 1] * 32),
            pipeline_status=PipelineStatus.ready,
            original_bucket=BUCKET if key else None,
            original_object_key=key,
            created_at=T0 + timedelta(days=n),
        )
    )
    await session.commit()


def _store(fs, key: str) -> None:
    fs.put_object(BUCKET, key, io.BytesIO(b"x"), 1)


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.asyncio
async def test_missing_originals_log_an_error_even_with_other_files_present(db_session, fs, tmp_path, caplog):
    """The watcher stored new mail before app started: the bucket is not empty, the old originals are gone."""
    await _add(db_session, 0, key="originals/docs/1/old-upload.pdf")
    await _add(db_session, 1, key="originals/2/old-mail.eml")
    await _add(db_session, 2, key="originals/3/new-mail.eml")
    _store(fs, "originals/3/new-mail.eml")
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is True
    [message] = _errors(caplog)
    assert message.startswith("2 of the 3 oldest documents")
    assert "docs/upgrade-runbook.md" in message and "MinIO" in message
    assert str((tmp_path / BUCKET).resolve()) in message


@pytest.mark.asyncio
async def test_all_sampled_originals_present_is_quiet(db_session, fs, caplog):
    for n in range(3):
        await _add(db_session, n, key=f"originals/docs/{n}/a.pdf")
        _store(fs, f"originals/docs/{n}/a.pdf")
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is False
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_the_sample_is_the_oldest_documents(db_session, fs, caplog):
    """At most MISSING_ORIGINALS_SAMPLE stats: a newer missing file beyond the sample is not looked at."""
    for n in range(MISSING_ORIGINALS_SAMPLE):
        await _add(db_session, n, key=f"originals/docs/{n}/a.pdf")
        _store(fs, f"originals/docs/{n}/a.pdf")
    await _add(db_session, MISSING_ORIGINALS_SAMPLE, key="originals/docs/newest/a.pdf")
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is False
    with patch.object(fs, "object_exists", wraps=fs.object_exists) as stat:
        await log_missing_originals(db_session)
    assert stat.call_count == MISSING_ORIGINALS_SAMPLE


@pytest.mark.asyncio
async def test_a_missing_bucket_directory_logs_the_error(db_session, fs, caplog):
    await _add(db_session, 0, key="originals/2/msg.eml")
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is True
    assert _errors(caplog)[0].startswith("1 of the 1 oldest")


@pytest.mark.asyncio
async def test_no_stored_originals_is_quiet(db_session, fs, caplog):
    """Watched-folder documents have no stored original; deleted ones don't count."""
    await _add(db_session, 1, key=None)
    await _add(db_session, 2, key="originals/docs/9/gone.pdf", status="deleted")
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.storage"):
        assert await log_missing_originals(db_session) is False
    assert _errors(caplog) == []


@pytest.mark.asyncio
async def test_minio_backend_skips_the_check(monkeypatch):
    monkeypatch.setattr(storage_mod, "get_storage", lambda: MagicMock(spec=storage_mod.MinIOBackend))
    session = MagicMock(execute=AsyncMock())
    assert await log_missing_originals(session) is False
    session.execute.assert_not_awaited()
