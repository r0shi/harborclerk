"""Search and passage-reading schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from harbor_clerk.api.schemas.scope import ScopeSpec
from harbor_clerk.api.schemas.source_ref import SourceRefOut


class SearchRequest(BaseModel):
    query: str
    k: int = Field(default=10, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    doc_id: str | None = None
    doc_ids: list[str] | None = Field(default=None, max_length=50)
    after: datetime | None = None
    before: datetime | None = None
    language: str | None = None
    mime_type: str | None = None
    metadata_filter: dict[str, Any] | None = None
    text_contains: str | None = None
    faceted: bool = False
    scope: ScopeSpec | None = None

    @model_validator(mode="after")
    def check_doc_id_mutual_exclusion(self):
        if self.doc_id is not None and self.doc_ids is not None:
            raise ValueError("Cannot specify both doc_id and doc_ids")
        return self


class ConflictSourceOut(BaseModel):
    doc_id: str
    title: str


class ScoreBreakdown(BaseModel):
    fts: float
    vector: float
    hybrid: float
    reranker: float | None = None


class SearchHitOut(BaseModel):
    chunk_id: str
    doc_id: str
    chunk_num: int
    chunk_text: str
    page_start: int | None = None
    page_end: int | None = None
    language: str
    ocr_used: bool
    ocr_confidence: float | None = None
    score: float
    doc_title: str | None = None
    score_breakdown: ScoreBreakdown | None = None
    source: SourceRefOut | None = None
    citation: str | None = None


class SearchResponse(BaseModel):
    hits: list[SearchHitOut]
    total_candidates: int = 0
    has_more: bool = False
    possible_conflict: bool = False
    conflict_sources: list[ConflictSourceOut] = []
    reranker_status: Literal["ok", "disabled", "failed"] = "disabled"


class FindAllRequest(BaseModel):
    query: str
    text_contains: str | None = None
    max_results: int = Field(default=100, ge=1)
    offset: int = Field(default=0, ge=0)
    presentation: Literal["brief", "full"] = "brief"
    sort_by: Literal["relevance", "date_desc", "date_asc"] = "relevance"
    after: datetime | None = None
    before: datetime | None = None
    doc_id: UUID | None = None
    doc_ids: list[UUID] | None = Field(default=None, max_length=50)
    language: str | None = None
    mime_type: str | None = None
    metadata_filter: dict[str, Any] | None = None
    scope: ScopeSpec | None = None

    @model_validator(mode="after")
    def check_doc_id_mutual_exclusion(self):
        if self.doc_id is not None and self.doc_ids is not None:
            raise ValueError("Cannot specify both doc_id and doc_ids")
        return self


class FindAllTopChunkOut(BaseModel):
    chunk_id: str | None = None
    text: str | None = None
    page: int | None = None
    heading: str | None = None


class FindAllHitOut(BaseModel):
    doc_id: str
    doc_title: str
    mime_type: str
    language: str | None = None
    score: float
    ingested_at: datetime | None = None
    page_range: str | None = None
    top_chunk: FindAllTopChunkOut | None = None
    source: SourceRefOut | None = None
    citation: str | None = None


class FindAllResponse(BaseModel):
    results: list[FindAllHitOut]
    total_matches: int = 0
    returned: int = 0
    offset: int = 0
    truncated: bool = False
    sort_by: Literal["relevance", "date_desc", "date_asc"] = "relevance"
    presentation: Literal["brief", "full"] = "brief"


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
    source: SourceRefOut | None = None
    citation: str | None = None


class ReadPassagesResponse(BaseModel):
    passages: list[PassageDetail]
