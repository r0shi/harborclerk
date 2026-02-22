"""Upload request/response schemas."""

from datetime import datetime

from pydantic import BaseModel


class UploadFileResult(BaseModel):
    upload_id: str
    filename: str
    size_bytes: int
    mime_type: str | None = None
    status: str  # "pending_confirmation" or "duplicate"
    duplicate_doc_id: str | None = None
    duplicate_version_id: str | None = None


class UploadResponse(BaseModel):
    files: list[UploadFileResult]


class ConfirmUploadRequest(BaseModel):
    upload_id: str
    action: str  # "new_document" or "new_version"
    existing_doc_id: str | None = None  # required if action=new_version


class ConfirmUploadResponse(BaseModel):
    doc_id: str
    version_id: str
    status: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    original_filename: str
    status: str
    doc_id: str | None = None
    version_id: str | None = None
    created_at: datetime
