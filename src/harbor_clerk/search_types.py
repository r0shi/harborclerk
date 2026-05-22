"""Search result dataclasses shared between search.py and search_rerank.py.

Kept in a separate module to avoid the circular import that would arise if
search_rerank.py imported from search.py (which imports from search_rerank.py).
"""

from dataclasses import dataclass, field


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
