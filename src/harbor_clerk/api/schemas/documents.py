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
    source_path: str | None = None
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
    summary: str | None = None
    summary_model: str | None = None
    source_path: str | None = None


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


class HeadingOut(BaseModel):
    level: int
    title: str
    page_num: int | None = None


class DocumentOutlineResponse(BaseModel):
    doc_id: str
    version_id: str
    title: str
    page_count: int
    chunk_count: int
    headings: list[HeadingOut]


class DateRange(BaseModel):
    oldest: datetime | None = None
    newest: datetime | None = None


class CorpusDocumentSummary(BaseModel):
    doc_id: str
    title: str
    summary: str | None = None
    status: str | None = None
    updated_at: datetime


class CorpusOverviewResponse(BaseModel):
    document_count: int
    total_chunks: int
    total_pages: int
    languages: dict[str, int]
    mime_types: dict[str, int]
    date_range: DateRange
    documents: list[CorpusDocumentSummary]
    truncated: bool
