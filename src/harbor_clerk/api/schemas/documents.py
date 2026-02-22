"""Document management schemas."""

from datetime import datetime

from pydantic import BaseModel


class JobInfo(BaseModel):
    job_id: str
    stage: str
    status: str
    progress_current: int | None = None
    progress_total: int | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class VersionInfo(BaseModel):
    version_id: str
    status: str
    mime_type: str | None = None
    size_bytes: int | None = None
    has_text_layer: bool | None = None
    needs_ocr: bool | None = None
    extracted_chars: int | None = None
    error: str | None = None
    created_at: datetime
    jobs: list[JobInfo] = []


class DocumentSummary(BaseModel):
    doc_id: str
    title: str
    canonical_filename: str | None = None
    status: str
    latest_version_status: str | None = None
    version_count: int
    created_at: datetime
    updated_at: datetime


class DocumentDetail(BaseModel):
    doc_id: str
    title: str
    canonical_filename: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime
    versions: list[VersionInfo] = []


class PageContent(BaseModel):
    page_num: int
    text: str
    ocr_used: bool
    ocr_confidence: float | None = None


class DocumentContentResponse(BaseModel):
    doc_id: str
    version_id: str
    pages: list[PageContent]
    total_chars: int
