from pathlib import Path
from unittest.mock import patch

from scripts.test_corpora.corpora import enron


def test_enron_filter_keeps_only_target_custodians(tmp_path: Path, fixtures_dir: Path):
    src = fixtures_dir / "enron_sample"

    # Pretend the "downloaded" corpus is just our fixture dir
    with patch.object(enron, "_download_corpus", return_value=src):
        m = enron.acquire(workdir=tmp_path / "work", custodians=["skilling", "lay"], random_count=1)
        assert m.doc_count == 3  # 1 skilling + 1 lay + 1 random
        names = sorted(p.name for p in m.ingest_dir.glob("*.eml"))
        assert "skilling_001.eml" in names
        assert "lay_001.eml" in names


def test_enron_marker_without_files_re_acquires(tmp_path: Path, fixtures_dir: Path):
    """Regression for the stale-marker bug: if the .acquired marker exists
    but the .eml files are gone (manual cleanup, fs eviction, lost mount),
    acquire() must re-download instead of trusting the marker. Without
    this guard, Phase 0 marks the unit DONE in 4ms and Phase 4 hits a
    120s 'watcher never enqueued' timeout."""
    src = fixtures_dir / "enron_sample"

    workdir = tmp_path / "work"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    # Pre-create the marker but leave ingest_dir empty
    (ingest_dir / ".acquired").write_text("acquired")
    assert list(ingest_dir.glob("*.eml")) == []

    with patch.object(enron, "_download_corpus", return_value=src):
        m = enron.acquire(workdir=workdir, custodians=["skilling", "lay"], random_count=1)

    # Should have re-acquired despite the marker
    assert m.doc_count == 3
    assert sorted(p.name for p in m.ingest_dir.glob("*.eml"))
    # Fresh marker should be in place after re-acquire
    assert (ingest_dir / ".acquired").exists()


def test_enron_marker_with_files_short_circuits(tmp_path: Path):
    """The fast path still works: marker + files present → return manifest
    without re-downloading. Asserted by patching _download_corpus to raise
    if it's called."""
    workdir = tmp_path / "work"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    (ingest_dir / "skilling_x.eml").write_text("From: skilling@enron.com\n\nbody")
    (ingest_dir / ".acquired").write_text("acquired")

    def _no_download(_workdir):
        raise AssertionError("must not download when marker + files exist")

    with patch.object(enron, "_download_corpus", side_effect=_no_download):
        m = enron.acquire(workdir=workdir)
        assert m.doc_count == 1
