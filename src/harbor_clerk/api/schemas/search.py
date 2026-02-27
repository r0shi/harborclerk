"""Search and passage-reading schemas."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class SearchRequest(BaseModel):
    query: str
    k: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    doc_id: str | None = None
    version_id: str | None = None
    doc_ids: list[str] | None = None
    after: datetime | None = None
    before: datetime | None = None
    language: str | None = None
    mime_type: str | None = None
    faceted: bool = False

    @model_validator(mode="after")
    def check_doc_id_mutual_exclusion(self):
        if self.doc_id is not None and self.doc_ids is not None:
            raise ValueError("Cannot specify both doc_id and doc_ids")
        return self


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
    total_candidates: int = 0
    has_more: bool = False
    possible_conflict: bool = False
    conflict_sources: list[ConflictSourceOut] = []


class FacetedDocGroup(BaseModel):
    doc_id: str
    doc_title: str | None = None
    top_score: float
    hit_count: int
    hits: list[SearchHitOut]


class FacetedSearchResponse(BaseModel):
    documents: list[FacetedDocGroup]
    total_candidates: int = 0
    has_more: bool = False


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
