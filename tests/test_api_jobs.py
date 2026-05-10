"""Tests for /api/jobs endpoints."""

from harbor_clerk.models import Document, IngestionJob
from harbor_clerk.models.enums import JobStage, JobStatus, PipelineStatus
from tests.conftest import auth_header


async def test_queue_snapshot_includes_summarizing_array(client, db_session, admin_user, admin_token):
    """Queue snapshot exposes in-flight summarize jobs as a separate
    `summarizing` array so the frontend can render them in their own
    section."""
    doc = Document(
        title="Summary in flight",
        canonical_filename="sum.pdf",
        status="active",
        sha256=b"s" * 32,
        pipeline_status=PipelineStatus.ready,
    )
    db_session.add(doc)
    await db_session.flush()

    db_session.add(
        IngestionJob(
            doc_id=doc.doc_id,
            stage=JobStage.summarize,
            status=JobStatus.running,
            progress_current=3,
            progress_total=9,
        )
    )
    await db_session.flush()

    response = await client.get("/api/jobs/snapshot", headers=auth_header(admin_token))
    assert response.status_code == 200
    data = response.json()
    assert "summarizing" in data
    assert len(data["summarizing"]) == 1
    job = data["summarizing"][0]
    assert job["doc_id"] == str(doc.doc_id)
    assert job["filename"] == "sum.pdf"
    assert job["status"] == "running"
    assert job["progress_current"] == 3
    assert job["progress_total"] == 9
