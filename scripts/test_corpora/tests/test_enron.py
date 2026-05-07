from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.test_corpora.corpora import enron


def _write_parquet_fixture(out_dir: Path, rows: list[dict]) -> None:
    """Materialize an HF-style parquet dataset under ``out_dir/data/``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, data_dir / "train-00000-of-00001.parquet")


def _make_row(file_name: str, *, body: str = "hi", subject: str = "x") -> dict:
    return {
        "message_id": f"<{file_name}@enron.com>",
        "subject": subject,
        "from": "alice@enron.com",
        "to": ["bob@enron.com"],
        "cc": [],
        "bcc": [],
        "date": datetime(2001, 5, 14, 23, 39, tzinfo=UTC),
        "body": body,
        "file_name": file_name,
    }


def test_enron_filter_keeps_only_target_custodians(tmp_path: Path):
    """Custodian filter is name-prefix on ``file_name``; non-custodian rows
    feed the random sample."""
    rows = [
        _make_row("skilling-j/_sent_mail/1."),
        _make_row("skilling-j/inbox/2."),
        _make_row("lay-k/_sent_mail/1."),
        _make_row("fastow-a/inbox/1."),
        _make_row("allen-p/_sent_mail/1."),
        _make_row("germany-c/inbox/1."),
        _make_row("dasovich-j/inbox/1."),
    ]
    fake_hf = tmp_path / "hf_enron"
    _write_parquet_fixture(fake_hf, rows)

    with patch.object(enron, "_download_corpus", return_value=fake_hf):
        m = enron.acquire(workdir=tmp_path / "work", custodians=["skilling", "lay"], random_count=1)

    # 2 skilling + 1 lay = 3 custodian, plus 1 random non-custodian = 4
    assert m.doc_count == 4
    names = sorted(p.name for p in m.ingest_dir.glob("*.eml"))
    assert any(n.startswith("skilling-j_") for n in names)
    assert any(n.startswith("lay-k_") for n in names)


def test_enron_eml_format_is_rfc822_like(tmp_path: Path):
    """The materialized .eml should have standard headers + blank line + body
    so Tika can parse it cleanly during HC ingest."""
    fake_hf = tmp_path / "hf_enron"
    _write_parquet_fixture(
        fake_hf,
        [
            {
                "message_id": "<m1@enron.com>",
                "subject": "Q4 forecast",
                "from": "phillip.allen@enron.com",
                "to": ["tim.belden@enron.com"],
                "cc": ["alice@enron.com"],
                "bcc": [],
                "date": datetime(2001, 5, 14, 23, 39, tzinfo=UTC),
                "body": "Here is our forecast",
                "file_name": "skilling-j/_sent_mail/1.",
            }
        ],
    )

    with patch.object(enron, "_download_corpus", return_value=fake_hf):
        m = enron.acquire(workdir=tmp_path / "work", custodians=["skilling"], random_count=0)

    eml = (m.ingest_dir / "skilling-j__sent_mail_1_.eml").read_text()
    assert "From: phillip.allen@enron.com" in eml
    assert "To: tim.belden@enron.com" in eml
    assert "Cc: alice@enron.com" in eml
    assert "Subject: Q4 forecast" in eml
    assert "Message-ID: <m1@enron.com>" in eml
    # Headers ↔ body separator
    assert "\n\nHere is our forecast" in eml


def test_enron_marker_without_files_re_acquires(tmp_path: Path):
    """Regression for the stale-marker bug: marker present but ingest_dir
    empty → re-acquire instead of returning a 0-doc manifest."""
    fake_hf = tmp_path / "hf_enron"
    _write_parquet_fixture(fake_hf, [_make_row("skilling-j/inbox/1.")])

    workdir = tmp_path / "work"
    ingest_dir = workdir / "ingest"
    ingest_dir.mkdir(parents=True)
    (ingest_dir / ".acquired").write_text("acquired")
    assert list(ingest_dir.glob("*.eml")) == []

    with patch.object(enron, "_download_corpus", return_value=fake_hf):
        m = enron.acquire(workdir=workdir, custodians=["skilling"], random_count=0)

    assert m.doc_count == 1
    assert (ingest_dir / ".acquired").exists()


def test_enron_marker_with_files_short_circuits(tmp_path: Path):
    """Fast path: marker + files present → return immediately, no download."""
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
