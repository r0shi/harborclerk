"""End-to-end: the extract stage runs the extractor framework and persists
results to Document.doc_metadata. Uses a sidecar JSON file next to the source
file — no Tika call needed for plain-text files so the test stays hermetic."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from harbor_clerk.db_sync import get_sync_session
from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus


@pytest.mark.asyncio
async def test_extract_stage_writes_sidecar_metadata_to_doc(db_session, tmp_path: Path):
    """run_extract on a .txt file with a companion sidecar .json populates
    doc.doc_metadata['sidecar'] and doc.doc_metadata['_source_provenance']."""
    # Set up source file + sidecar
    source = tmp_path / "0001_invoice.txt"
    source.write_text("Invoice body text")
    sidecar = tmp_path / "0001_invoice.json"
    sidecar.write_text(json.dumps({"vendor": "Acme", "total_usd": 100}))

    doc = Document(
        title="0001_invoice",
        canonical_filename="0001_invoice.txt",
        status="active",
        sha256=hashlib.sha256(source.read_bytes()).digest(),
        pipeline_status=PipelineStatus.queued,
        mime_type="text/plain",
        source_path=str(source),
    )
    db_session.add(doc)
    await db_session.flush()
    db_session.add(IngestionJob(doc_id=doc.doc_id, stage=JobStage.extract, status=JobStatus.queued))
    await db_session.commit()
    doc_id = doc.doc_id

    # run_extract is sync — call directly (same pattern as test_pipeline.py).
    from harbor_clerk.worker.stages.extract import run_extract

    run_extract(doc_id)

    # Re-fetch via a fresh sync session (run_extract commits its own session).
    sync_session = get_sync_session()
    try:
        refreshed = sync_session.execute(select(Document).where(Document.doc_id == doc_id)).scalar_one()
        metadata = refreshed.doc_metadata
    finally:
        sync_session.close()

    assert "sidecar" in metadata, f"expected 'sidecar' key in doc_metadata, got: {metadata}"
    assert metadata["sidecar"] == {"vendor": "Acme", "total_usd": 100}
    assert "_source_provenance" in metadata
    assert "sidecar" in metadata["_source_provenance"]
