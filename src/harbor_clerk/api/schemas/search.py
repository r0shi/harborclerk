"""Search and passage-reading schemas."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from harbor_clerk.api.schemas.scope import ScopeSpec
from harbor_clerk.api.schemas.source_ref import SourceRefOut

# Whether a search's vector leg ran: "unavailable" means the embedder (or the
# vector query) failed and the results are keyword matches only; "partial" that
# it ran but under 90% of the chunks of the documents this search covers have a
# vector, so some documents are findable by keyword only. semantic_coverage is
# that share: of the corpus without a document filter, of the filtered
# documents with a filter of at most 50; null when the vector leg failed, the
# filter is larger, or there is nothing to measure.
SemanticStatus = Literal["ok", "partial", "unavailable"]


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
    summary_state: Literal["has", "missing", "failed", "pending"] | None = None
    pipeline_status: str | None = None
    job_stage: Literal["extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize"] | None = None
    job_status: Literal["queued", "running", "done", "error"] | None = None
    job_issue: (
        Literal[
            "any_issue",
            "failed_job",
            "ocr_failed",
            "entity_skipped",
            "summary_failed",
            "summary_blocked",
            "summary_pending",
            "status_cleanup",
        ]
        | None
    ) = None
    faceted: bool = False
    scope: ScopeSpec | None = None

    # Kept as str and checked with uuid.UUID, the parser the route uses, rather than typed
    # UUID: pydantic's parser refuses a few forms uuid.UUID accepts ("uuid:" prefix, other
    # hyphen placement), and an empty doc_id means no filter. A malformed id is a 422 here
    # instead of a ValueError (500) in the route (#873).
    @field_validator("doc_id")
    @classmethod
    def check_doc_id_is_uuid(cls, v: str | None) -> str | None:
        if v:
            try:
                UUID(v)
            except ValueError:
                raise ValueError("doc_id must be a UUID") from None
        return v

    @field_validator("doc_ids")
    @classmethod
    def check_doc_ids_are_uuids(cls, v: list[str] | None) -> list[str] | None:
        for i, d in enumerate(v or []):
            try:
                UUID(d)
            except ValueError:
                raise ValueError(f"doc_ids[{i}] must be a UUID") from None
        return v

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
    # The chunk's first N characters under an API key's snippet cap; null at a cap of 0 (#823).
    chunk_text: str | None
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
    # "unavailable": the vector leg failed and the hits are keyword-only (#685).
    semantic_status: SemanticStatus = "ok"
    semantic_coverage: float | None = None


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
    summary_state: Literal["has", "missing", "failed", "pending"] | None = None
    pipeline_status: str | None = None
    job_stage: Literal["extract", "ocr", "chunk", "entities", "embed", "summarize", "finalize"] | None = None
    job_status: Literal["queued", "running", "done", "error"] | None = None
    job_issue: (
        Literal[
            "any_issue",
            "failed_job",
            "ocr_failed",
            "entity_skipped",
            "summary_failed",
            "summary_blocked",
            "summary_pending",
            "status_cleanup",
        ]
        | None
    ) = None
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
    # presentation="brief": a bounded excerpt of the best chunk; "full" sets top_chunk instead.
    snippet: str | None = None
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
    semantic_status: SemanticStatus = "ok"
    semantic_coverage: float | None = None


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
    semantic_status: SemanticStatus = "ok"
    semantic_coverage: float | None = None


class ReadPassagesRequest(BaseModel):
    chunk_ids: list[str] = Field(..., min_length=1, max_length=50)
    include_context: bool = False


class PassageDetail(BaseModel):
    chunk_id: str
    doc_id: str
    chunk_num: int
    # The chunk's first N characters under an API key's snippet cap (context_before/after too); null at 0.
    chunk_text: str | None
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
