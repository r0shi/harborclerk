import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.test_corpora.corpora import synthetic


def test_synthetic_acquire_writes_doc_and_sidecar(tmp_path: Path):
    fake_anthropic = MagicMock()
    # Mock returns a templated invoice
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text='{"text": "INVOICE\\nVendor: Acme\\nTotal: $12,500", "facts": {"vendor": "Acme", "total_usd": 12500}}')
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
