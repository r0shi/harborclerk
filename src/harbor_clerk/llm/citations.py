"""Citation extraction from MCP tool results.

The chat and research loops execute tools whose JSON output contains
references back to corpus chunks and documents. This module pulls those
references into a uniform ``{doc_id, doc_title?, chunk_id?, pages?, score?}``
shape so that the chat ``done`` SSE event and ``ResearchDetail`` can expose a
``citations`` list to clients (UI badges, test-harness ``citation_overlap``,
etc.).

The extractor is best-effort: tool results that don't look like citations
(errors, status responses, free-form summaries) return an empty list. It
intentionally never raises on malformed input — citation metadata is
auxiliary; a parse failure must not break a chat turn.
"""

from __future__ import annotations

import json
from typing import Any


def extract_citations_from_tool_result(raw_result: str) -> list[dict]:
    """Pull citation-shaped records out of one tool-result JSON string.

    Recognised shapes (the seven that any HC ``kb_*`` tool can emit):

    - ``kb_search`` → ``{"hits": [{"doc_id", "chunk_id", "doc_title", "pages", "score", ...}]}``
    - ``kb_batch_search`` → ``{"results": [{"hits": [...]}]}``
    - ``kb_read_passages`` → ``{"passages": [{"chunk_id", "doc_title", "pages", ...}]}``
      (no top-level ``doc_id``; passages carry chunk_id only)
    - ``kb_expand_context`` → ``{"doc_id", "doc_title", "chunks": [{"chunk_id", "pages", ...}]}``
    - ``kb_list_recent`` / ``kb_corpus_overview`` → ``{"documents": [{"doc_id", "title", ...}]}``
    - ``kb_find_related`` → ``{"doc_id", "related": [{"doc_id", "title", ...}]}``
    - ``kb_get_document`` / ``kb_document_outline`` → ``{"doc_id", "title", ...}`` (top-level)

    Anything else (e.g. ``{"error": ...}``, ``kb_system_health``,
    ``kb_entity_overview``, ``kb_corpus_topics``) returns ``[]``.

    Returns a list of fresh dicts; the caller owns dedup and ordering.
    """
    try:
        data: Any = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, dict) or "error" in data:
        return []

    out: list[dict] = []

    parent_doc_id = data.get("doc_id")
    parent_doc_title = data.get("doc_title") or data.get("title")

    def _record(d: dict, *, fallback_doc_id: Any = None, fallback_doc_title: Any = None) -> None:
        doc_id = d.get("doc_id") or fallback_doc_id
        if doc_id is None:
            # passages-only case: chunk_id present, no doc_id. Capture the
            # chunk so a chunk→doc join later can still resolve it; the
            # test harness will ignore entries with no doc_id when computing
            # citation_overlap.
            if not d.get("chunk_id"):
                return
            cite: dict = {}
        else:
            cite = {"doc_id": str(doc_id)}

        title = d.get("doc_title") or d.get("title") or fallback_doc_title
        if title:
            cite["doc_title"] = title
        chunk_id = d.get("chunk_id")
        if chunk_id:
            cite["chunk_id"] = str(chunk_id)
        pages = d.get("pages")
        if pages:
            cite["pages"] = pages
        score = d.get("score")
        if isinstance(score, (int, float)):
            cite["score"] = score
        out.append(cite)

    # kb_search: top-level "hits"
    for h in data.get("hits") or []:
        if isinstance(h, dict):
            _record(h)

    # kb_batch_search: {"results": [{"hits": [...]}]}
    for r in data.get("results") or []:
        if isinstance(r, dict):
            for h in r.get("hits") or []:
                if isinstance(h, dict):
                    _record(h)

    # kb_read_passages: {"passages": [{chunk_id, doc_title, pages, ...}]}
    for p in data.get("passages") or []:
        if isinstance(p, dict):
            _record(p)

    # kb_expand_context: {"doc_id", "chunks": [...]} — chunks inherit parent doc_id
    for c in data.get("chunks") or []:
        if isinstance(c, dict):
            _record(c, fallback_doc_id=parent_doc_id, fallback_doc_title=parent_doc_title)

    # kb_list_recent / kb_corpus_overview: {"documents": [{doc_id, title, ...}]}
    for d in data.get("documents") or []:
        if isinstance(d, dict):
            _record(d)

    # kb_find_related: {"doc_id", "related": [{doc_id, title, ...}]}
    for r in data.get("related") or []:
        if isinstance(r, dict):
            _record(r)

    # kb_get_document / kb_document_outline / single-doc responses:
    # doc_id at top level with no nested list we already handled.
    has_nested = any(data.get(k) for k in ("hits", "results", "passages", "chunks", "documents", "related"))
    if parent_doc_id and not has_nested:
        _record({"doc_id": parent_doc_id, "doc_title": parent_doc_title})

    return out


def dedupe_citations(cites: list[dict]) -> list[dict]:
    """Dedupe by (doc_id, chunk_id), preserving first-seen order.

    When the same (doc_id, chunk_id) appears more than once, keep the entry
    with the highest score (search calls report scores; ``kb_read_passages``
    does not). Entries with no doc_id and no chunk_id are dropped — they
    can't be deduplicated meaningfully and aren't useful as citations.
    """
    seen: dict[tuple, dict] = {}
    order: list[tuple] = []
    for c in cites:
        if not isinstance(c, dict):
            continue
        key = (c.get("doc_id"), c.get("chunk_id"))
        if key == (None, None):
            continue
        if key in seen:
            prev = seen[key]
            cur_score = c.get("score")
            prev_score = prev.get("score")
            if isinstance(cur_score, (int, float)) and (
                not isinstance(prev_score, (int, float)) or cur_score > prev_score
            ):
                seen[key] = c
        else:
            seen[key] = c
            order.append(key)
    return [seen[k] for k in order]
