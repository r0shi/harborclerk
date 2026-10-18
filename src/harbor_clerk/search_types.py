"""Search result dataclasses shared between search.py and search_rerank.py.

Kept in a separate module to avoid the circular import that would arise if
search_rerank.py imported from search.py (which imports from search_rerank.py).
"""

from dataclasses import dataclass, field
from datetime import datetime

SEMANTIC_OK = "ok"
SEMANTIC_UNAVAILABLE = "unavailable"
# The query embedded, but the candidate documents' chunks are mostly without
# vectors (an embed still pending, deferred or failed), so the semantic leg
# saw only part of what keyword search found.
SEMANTIC_PARTIAL = "partial"
# Below this share of candidate-document chunks with a vector, a result is
# "partial". Ten percent missing is already a document or two invisible to
# the semantic leg on a typical page; a fully embedded corpus reads 1.0.
SEMANTIC_PARTIAL_COVERAGE = 0.9

# Said alongside a degraded result on the surfaces a model reads (MCP, chat,
# research). In #685 research reported "0 unique passages" for termination
# notice periods across a corpus of contracts, and three models concluded the
# corpus did not discuss it: a keyword-only result read as a complete one.
SEMANTIC_UNAVAILABLE_NOTICE = (
    "Semantic search is unavailable (the embedding service failed), so these results are keyword matches only. "
    "Passages that use different wording are missed: absence here is not evidence the corpus lacks the topic."
)
SEMANTIC_PARTIAL_NOTICE = (
    "Semantic search covered only part of these documents: many of their passages are not embedded yet, so "
    "those are found by keyword only. Passages that use different wording may be missed."
)


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
    # "ok", or "unavailable" when the vector leg failed (the query could not be
    # embedded, or the vector query errored) and the hits are keyword-only.
    semantic_status: str = SEMANTIC_OK
    # Share of the candidate documents' chunks with a vector; None when the
    # vector leg failed or nothing was found.
    semantic_coverage: float | None = None


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
    # presentation="brief": a bounded one-line excerpt of the best chunk,
    # centred on text_contains when given (search._brief_snippet).
    snippet: str | None = None
    snippet_chunk_id: str | None = None  # the chunk the snippet was cut from


@dataclass
class FindAllResult:
    """Result of find_all() — doc-level deduped, sortable, paginated."""

    hits: list[FindAllHit]
    total_matches: int  # post-dedupe, post-filter count of unique docs in
    # the candidate pool (bounded by internal_k=5*(offset+max_results))
    offset: int  # echo of the input
    truncated: bool  # total_matches > offset + len(hits)
    sort_by: str  # echo of the input
    presentation: str  # echo of the input
    semantic_status: str = SEMANTIC_OK  # the inner hybrid_search's, see SearchResult
    semantic_coverage: float | None = None
