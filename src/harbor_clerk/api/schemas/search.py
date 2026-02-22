"""Search and passage-reading schemas."""

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    query: str
    k: int = Field(default=10, ge=1, le=100)
    doc_id: str | None = None
    version_id: str | None = None


class ConflictSourceOut(BaseModel):
    doc_id: str
    version_id: str
    title: str


class SearchHitOut(BaseModel):
    chunk_id: str
    doc_id: str
    version_id: str
    chunk_num: int
    chunk_text: str
    page_start: int | None = None
    page_end: int | None = None
    language: str
    ocr_used: bool
    ocr_confidence: float | None = None
    score: float
    doc_title: str | None = None


class SearchResponse(BaseModel):
    hits: list[SearchHitOut]
    possible_conflict: bool = False
    conflict_sources: list[ConflictSourceOut] = []


class ReadPassagesRequest(BaseModel):
    chunk_ids: list[str] = Field(..., min_length=1, max_length=50)
    include_context: bool = False


class PassageDetail(BaseModel):
    chunk_id: str
    doc_id: str
    version_id: str
    chunk_num: int
    chunk_text: str
    page_start: int | None = None
    page_end: int | None = None
    language: str
    ocr_used: bool
    ocr_confidence: float | None = None
    doc_title: str | None = None
    context_before: str | None = None
    context_after: str | None = None


class ReadPassagesResponse(BaseModel):
    passages: list[PassageDetail]
