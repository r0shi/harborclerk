import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.test_corpora.corpora import cuad


def _make_fake_release(path: Path) -> None:
    """Build a tiny .zip that mimics the CUAD release layout."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for i in range(5):
            data = b"%PDF-1.4\nfake contract " + str(i).encode()
            z.writestr(f"CUAD_v1/contracts/contract_{i:03d}.pdf", data)


def test_cuad_acquire_idempotent(tmp_path: Path):
    archive = tmp_path / "cuad-fake.zip"
    _make_fake_release(archive)

    with patch.object(cuad, "_download_release", return_value=archive):
        m1 = cuad.acquire(workdir=tmp_path / "work", sample_size=3)
        assert m1.doc_count == 3
        assert (m1.ingest_dir / "contract_000.pdf").exists()

        # Second call must not re-download
        with patch.object(cuad, "_download_release", side_effect=AssertionError("re-downloaded")):
            m2 = cuad.acquire(workdir=tmp_path / "work", sample_size=3)
            assert m2.ingest_dir == m1.ingest_dir


def test_cuad_marker_without_files_re_acquires(tmp_path: Path):
    """Regression for the stale-marker bug: marker present but ingest_dir
    empty → must re-download. Without this guard, Phase 0 returns a
    manifest with doc_count=0 and Phase 4 hits a 120s ingest-watcher
    timeout."""
    archive = tmp_path / "cuad-fake.zip"
    _make_fake_release(archive)

    workdir = tmp_path / "work"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    # Pre-create the marker but leave ingest_dir empty
    (ingest_dir / ".acquired").write_text("acquired")
    assert list(ingest_dir.glob("*.pdf")) == []

    with patch.object(cuad, "_download_release", return_value=archive):
        m = cuad.acquire(workdir=workdir, sample_size=3)

    # Should have re-acquired despite the marker
    assert m.doc_count == 3
    assert (m.ingest_dir / "contract_000.pdf").exists()
