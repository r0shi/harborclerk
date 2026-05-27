"""Search result dataclasses shared between search.py and search_rerank.py.

Kept in a separate module to avoid the circular import that would arise if
search_rerank.py imported from search.py (which imports from search_rerank.py).
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ConflictSource:
    doc_id: str
    title: str


@dataclass
class SearchHit:
    chunk_id: str
    doc_id: str
    chunk_num: int
    chunk_text: str
    page_start: int | None
    page_end: int | None
    language: str
    ocr_used: bool
    ocr_confidence: float | None
    score: float
    doc_title: str | None = None


@dataclass
class SearchResult:
    hits: list[SearchHit]
    total_candidates: int = 0
    possible_conflict: bool = False
    conflict_sources: list[ConflictSource] = field(default_factory=list)
    reranker_status: str = "disabled"


@dataclass
class FindAllHit:
    """One row in a FindAllResult — a document, with its best chunk's score."""

    doc_id: str
    doc_title: str
    mime_type: str
    language: str | None
    score: float  # max chunk score for this doc
    ingested_at: datetime  # documents.created_at
    page_range: str | None  # e.g. "1-12"; None if unknown
    top_chunk_id: str | None  # for presentation="full"
    top_chunk_text: str | None  # for presentation="full"
    top_chunk_page: int | None
    top_chunk_heading: str | None


@dataclass
class FindAllResult:
    """Result of find_all() — doc-level deduped, sortable, paginated."""

    hits: list[FindAllHit]
    total_matches: int  # post-dedupe, post-filter count of unique docs
    offset: int  # echo of the input
    truncated: bool  # total_matches > offset + len(hits)
    sort_by: str  # echo of the input
    presentation: str  # echo of the input
