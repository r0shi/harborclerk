import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.test_corpora.corpora import synthetic


def test_synthetic_acquire_writes_doc_and_sidecar(tmp_path: Path):
    fake_anthropic = MagicMock()
    # Mock returns a templated invoice
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(
            text='{"text": "INVOICE\\nVendor: Acme\\nTotal: $12,500", "facts": {"vendor": "Acme", "total_usd": 12500}}'
        )
    ]

    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(
            workdir=tmp_path / "synth",
            doc_counts={"invoice": 2},  # only 2 docs to keep the test fast
            ocr_subset_count=0,
        )
    assert m.doc_count == 2
    docs = sorted(m.ingest_dir.glob("*.txt"))
    assert len(docs) == 2
    # Each doc has a JSON sidecar with ground-truth facts
    sidecars = sorted(m.ingest_dir.glob("*.json"))
    assert len(sidecars) == 2
    facts = json.loads(sidecars[0].read_text())
    assert facts["vendor"] == "Acme"


def test_synthetic_marker_without_files_re_acquires(tmp_path: Path):
    """Regression for the stale-marker bug: marker present but ingest_dir
    has no .txt/.pdf docs → must regenerate. Without this guard, Phase 0
    returns a manifest with doc_count=0 and Phase 4 hits a 120s
    ingest-watcher timeout."""
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text='{"text": "INVOICE\\nVendor: Acme", "facts": {"vendor": "Acme", "total_usd": 100}}')
    ]

    workdir = tmp_path / "synth"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    (ingest_dir / ".acquired").write_text("acquired")
    # No .txt/.pdf files — only the marker. Stale-cache scenario.

    with patch.object(synthetic, "_make_client", return_value=fake_anthropic):
        m = synthetic.acquire(
            workdir=workdir,
            doc_counts={"invoice": 2},
            ocr_subset_count=0,
        )

    assert m.doc_count == 2
    assert sorted(m.ingest_dir.glob("*.txt"))
