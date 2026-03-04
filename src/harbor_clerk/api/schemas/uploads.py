"""Upload request/response schemas."""

from datetime import datetime

from pydantic import BaseModel


class UploadFileResult(BaseModel):
    upload_id: str
    filename: str
    size_bytes: int
    mime_type: str | None = None
    status: str  # "pending_confirmation", "duplicate", or "skipped"
    duplicate_doc_id: str | None = None
    duplicate_version_id: str | None = None


class UploadResponse(BaseModel):
    files: list[UploadFileResult]


class ConfirmUploadRequest(BaseModel):
    upload_id: str
    action: str  # "new_document" or "new_version"
    existing_doc_id: str | None = None  # required if action=new_version
    source_path: str | None = None


class ConfirmUploadResponse(BaseModel):
    doc_id: str
    version_id: str
    status: str


class BatchConfirmItem(BaseModel):
    upload_id: str
    action: str  # "new_document" | "new_version"
    existing_doc_id: str | None = None
    source_path: str | None = None


class BatchConfirmRequest(BaseModel):
    items: list[BatchConfirmItem]


class BatchConfirmResultItem(BaseModel):
    upload_id: str
    doc_id: str | None = None
    version_id: str | None = None
    status: str
    error: str | None = None


class BatchConfirmResponse(BaseModel):
    results: list[BatchConfirmResultItem]


class UploadStatusResponse(BaseModel):
    upload_id: str
    original_filename: str
    status: str
    doc_id: str | None = None
    version_id: str | None = None
    created_at: datetime


# --- Upload sessions ---


class CreateSessionRequest(BaseModel):
    total_files: int
    auto_confirm: bool = False
    label: str | None = None


class SessionResponse(BaseModel):
    session_id: str
    user_id: str
    label: str | None = None
    auto_confirm: bool
    status: str
    total_files: int
    uploaded: int
    confirmed: int
    failed: int
    created_at: datetime
    updated_at: datetime


class SessionFileUploadResponse(BaseModel):
    upload_id: str
    source_path: str | None = None
    status: str  # "pending_confirmation", "duplicate", "processing"
    sha256: str
    filename: str
    size_bytes: int
    duplicate_doc_id: str | None = None
    duplicate_version_id: str | None = None
    doc_id: str | None = None
    version_id: str | None = None


class ResumeResponse(BaseModel):
    completed_paths: list[str]
