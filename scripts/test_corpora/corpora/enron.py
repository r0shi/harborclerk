"""Enron Email Corpus subset acquisition.

Downloads a HuggingFace-hosted Enron dataset, filters to target custodian
inboxes (Skilling, Lay, Fastow by default) plus a deterministic random
sample for noise. Outputs ``.eml`` files into ``ingest_dir``.

This module supports two acquisition paths:
1. ``_download_corpus`` (production) — fetches from HuggingFace.
2. Tests inject a path directly via the patched ``_download_corpus``.

The "filter to custodians" predicate is name-prefix based: filenames must
start with one of the configured custodian tokens (case-insensitive) to
count.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

from .manifest import CorpusManifest

CUSTODIANS_DEFAULT = ["skilling", "lay", "fastow"]
RANDOM_COUNT_DEFAULT = 500
RANDOM_SEED = 42


def _download_corpus(workdir: Path) -> Path:
    """Production path: download the Enron HuggingFace dataset."""
    # Implementation note: the harness depends on huggingface_hub which is
    # already in the harbor-clerk venv. Tests patch this whole function.
    from huggingface_hub import snapshot_download  # local import for testability

    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "hf_enron"
    if out.exists():
        return out
    snapshot_download(repo_id="corbt/enron-emails", repo_type="dataset", local_dir=str(out))
    return out


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
        return CorpusManifest(
            corpus_id="enron",
            ingest_dir=ingest_dir,
            doc_count=len(emls),
            total_size_bytes=sum(p.stat().st_size for p in emls),
            license="public domain",
            notes=f"Enron subset: {len(emls)} emails",
        )

    src = _download_corpus(workdir)
    all_eml = sorted(src.rglob("*.eml"))

    target: list[Path] = []
    others: list[Path] = []
    for p in all_eml:
        name = p.name.lower()
        if any(name.startswith(c) for c in custodians):
            target.append(p)
        else:
            others.append(p)

    rng = random.Random(RANDOM_SEED)
    sampled_others = rng.sample(others, min(random_count, len(others)))
    selected = target + sampled_others

    ingest_dir.mkdir(parents=True, exist_ok=True)
    for src_path in selected:
        shutil.copy2(src_path, ingest_dir / src_path.name)

    marker.write_text("acquired")
    return CorpusManifest(
        corpus_id="enron",
        ingest_dir=ingest_dir,
        doc_count=len(selected),
        total_size_bytes=sum(p.stat().st_size for p in selected),
        license="public domain",
        notes=f"Enron subset: {len(target)} custodian + {len(sampled_others)} random emails",
    )
