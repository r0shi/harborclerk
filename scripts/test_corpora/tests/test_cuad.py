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
