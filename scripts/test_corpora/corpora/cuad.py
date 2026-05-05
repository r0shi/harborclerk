"""CUAD (Contract Understanding Atticus Dataset) acquisition.

Downloads the release archive, extracts it, samples N contracts in a
deterministic order, and copies them to the ingest directory. Idempotent
via marker file ``.acquired``.

The fixture test patches ``_download_release`` to return a local tar.gz,
so the test does not hit the network.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import httpx

from .manifest import CorpusManifest

# Canonical mirror. If this URL goes 404, edit it; the deterministic file
# layout test (CUAD_v1/contracts/*.pdf) is what matters downstream.
CUAD_RELEASE_URL = "https://zenodo.org/record/4595826/files/CUAD_v1.zip"
SAMPLE_SIZE_DEFAULT = 80


def _download_release(workdir: Path) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    archive = workdir / "cuad_v1.zip"
    if archive.exists():
        return archive
    with (
        httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=30, read=600, write=30, pool=30),
        ) as c,
        c.stream("GET", CUAD_RELEASE_URL) as r,
    ):
        r.raise_for_status()
        with archive.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 1024):
                f.write(chunk)
    return archive


def _extract(archive: Path, workdir: Path) -> Path:
    extracted = workdir / "extracted"
    if extracted.exists():
        return extracted
    extracted.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as z:
        z.extractall(extracted)
    return extracted


def _gather_pdfs(extracted: Path) -> list[Path]:
    return sorted(extracted.rglob("*.pdf"))


def acquire(workdir: Path, sample_size: int = SAMPLE_SIZE_DEFAULT) -> CorpusManifest:
    workdir = Path(workdir)
    ingest_dir = workdir / "ingest"
    marker = ingest_dir / ".acquired"

    if marker.exists():
        pdfs = sorted(ingest_dir.glob("*.pdf"))
        return CorpusManifest(
            corpus_id="cuad",
            ingest_dir=ingest_dir,
            doc_count=len(pdfs),
            total_size_bytes=sum(p.stat().st_size for p in pdfs),
            license="CC-BY 4.0",
            notes=f"CUAD sample of {len(pdfs)} contracts",
        )

    archive = _download_release(workdir)
    extracted = _extract(archive, workdir)
    pdfs = _gather_pdfs(extracted)
    if len(pdfs) < sample_size:
        sample_size = len(pdfs)
    sampled = pdfs[:sample_size]  # deterministic by sorted name

    ingest_dir.mkdir(parents=True, exist_ok=True)
    for src in sampled:
        shutil.copy2(src, ingest_dir / src.name)

    marker.write_text("acquired")
    return CorpusManifest(
        corpus_id="cuad",
        ingest_dir=ingest_dir,
        doc_count=len(sampled),
        total_size_bytes=sum(p.stat().st_size for p in sampled),
        license="CC-BY 4.0",
        notes=f"CUAD sample of {len(sampled)} contracts (deterministic by filename)",
    )
