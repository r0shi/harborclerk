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
