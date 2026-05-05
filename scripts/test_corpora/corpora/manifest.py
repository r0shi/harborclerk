"""Common dataclass returned by every corpus's ``acquire()``."""

from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass
class CorpusManifest:
    corpus_id: str
    ingest_dir: Path
    doc_count: int
    total_size_bytes: int
    license: str
    notes: str = ""
    ground_truth: dict | None = None
