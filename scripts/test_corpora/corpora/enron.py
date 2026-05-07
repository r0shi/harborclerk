"""Enron Email Corpus subset acquisition.

Downloads the HuggingFace-hosted ``corbt/enron-emails`` dataset, which is
a parquet-format collection of ~517k emails (3 parquet files, ~493 MB).
Filters to target custodian inboxes (Skilling, Lay, Fastow by default)
plus a deterministic random sample for noise. Materializes each
selected row as an RFC 822-style ``.eml`` file in ``ingest_dir`` so the
HC watcher (which is filename-based) can pick them up.

This module supports two acquisition paths:
1. ``_download_corpus`` (production) — fetches the parquet dataset from
   HuggingFace.
2. Tests inject a path directly via the patched ``_download_corpus``
   pointing at a hand-crafted parquet fixture.

The "filter to custodians" predicate is name-prefix based: each parquet
row's ``file_name`` field (e.g. ``skilling-j/_sent_mail/1.``) must start
with one of the configured custodian tokens (case-insensitive).
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from pathlib import Path

from .manifest import CorpusManifest

CUSTODIANS_DEFAULT = ["skilling", "lay", "fastow"]
RANDOM_COUNT_DEFAULT = 500
RANDOM_SEED = 42


def _download_corpus(workdir: Path) -> Path:
    """Production path: download the Enron HuggingFace dataset."""
    from huggingface_hub import snapshot_download  # local import for testability

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "hf_enron"
    if out.exists():
        return out
    snapshot_download(repo_id="corbt/enron-emails", repo_type="dataset", local_dir=str(out))
    return out


def _iter_parquet_rows(dataset_root: Path) -> Iterator[dict]:
    """Yield each email row from every parquet file under ``dataset_root``.

    HuggingFace lays the dataset out as ``data/train-NNNNN-of-MMMMM.parquet``;
    we look there first and fall back to a recursive glob so callers can
    point at non-standard layouts (used by tests).
    """
    import pyarrow.parquet as pq

    candidates = sorted((dataset_root / "data").glob("*.parquet"))
    if not candidates:
        candidates = sorted(dataset_root.rglob("*.parquet"))
    for pq_file in candidates:
        yield from pq.read_table(pq_file).to_pylist()


def _safe_filename(file_name: str | None, idx: int) -> str:
    """Convert the dataset's ``file_name`` (e.g. ``skilling-j/_sent_mail/1.``)
    into a filesystem-safe basename. Trailing index disambiguates duplicates."""
    fn = file_name or f"unknown_{idx:06d}"
    return fn.replace("/", "_").replace(".", "_") or f"unknown_{idx:06d}"


def _row_to_eml(row: dict) -> str:
    """Format a parquet row as an RFC 822-ish ``.eml`` string. Apache Tika
    (HC's text extractor) parses this layout reliably for headers + body."""
    headers = [
        f"From: {row.get('from') or ''}",
        f"To: {', '.join(row.get('to') or [])}",
        f"Cc: {', '.join(row.get('cc') or [])}",
        f"Subject: {row.get('subject') or ''}",
        f"Date: {row.get('date')}",
        f"Message-ID: {row.get('message_id') or ''}",
    ]
    body = row.get("body") or ""
    return "\n".join(headers) + "\n\n" + body


def acquire(
    workdir: Path,
    custodians: list[str] | None = None,
    random_count: int = RANDOM_COUNT_DEFAULT,
) -> CorpusManifest:
    workdir = Path(workdir)
    custodians = [c.lower() for c in (custodians or CUSTODIANS_DEFAULT)]
    ingest_dir = workdir / "ingest"
    marker = ingest_dir / ".acquired"

    if marker.exists():
        emls = sorted(ingest_dir.glob("*.eml"))
        # Marker without files is a stale-cache sentinel — fall through to the
        # download/filter/copy path so the marker doesn't trick the caller into
        # thinking a corpus exists when its files are gone.
        if emls:
            return CorpusManifest(
                corpus_id="enron",
                ingest_dir=ingest_dir,
                doc_count=len(emls),
                total_size_bytes=sum(p.stat().st_size for p in emls),
                license="public domain",
                notes=f"Enron subset: {len(emls)} emails",
            )
        marker.unlink()  # remove stale marker so future runs don't keep tripping it

    src = _download_corpus(workdir)

    target: list[dict] = []
    others: list[dict] = []
    for row in _iter_parquet_rows(src):
        fn = (row.get("file_name") or "").lower()
        if any(fn.startswith(c) for c in custodians):
            target.append(row)
        else:
            others.append(row)

    rng = random.Random(RANDOM_SEED)
    sampled_others = rng.sample(others, min(random_count, len(others)))
    selected = target + sampled_others

    ingest_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for i, row in enumerate(selected):
        base = _safe_filename(row.get("file_name"), i)
        # Different rows can share file_name in some Enron layouts (forwarded
        # threads, etc.); disambiguate with the running index.
        if base in seen:
            base = f"{base}_{i:06d}"
        seen.add(base)
        (ingest_dir / f"{base}.eml").write_text(_row_to_eml(row), encoding="utf-8")

    marker.write_text("acquired")
    total_size = sum(p.stat().st_size for p in ingest_dir.glob("*.eml"))
    return CorpusManifest(
        corpus_id="enron",
        ingest_dir=ingest_dir,
        doc_count=len(selected),
        total_size_bytes=total_size,
        license="public domain",
        notes=f"Enron subset: {len(target)} custodian + {len(sampled_others)} random emails (from parquet)",
    )
