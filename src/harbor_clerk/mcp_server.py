"""MCP server — tools for Claude to query the knowledge base."""

import contextvars
import json
import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from harbor_clerk.api.deps import Principal
from harbor_clerk.auth import API_KEY_PREFIXES, decode_token, hash_api_key
from harbor_clerk.config import get_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.models import (
    ApiKey,
    Chunk,
    Document,
    DocumentHeading,
    DocumentPage,
    DocumentVersion,
    Entity,
    IngestionJob,
)
from harbor_clerk.models.enums import JobStage, VersionStatus
from harbor_clerk.oauth import validate_access_token as validate_oauth_access_token
from harbor_clerk.search import SearchHit, SearchResult, hybrid_search

logger = logging.getLogger(__name__)

# Runtime stats for kb_search usage monitoring
_search_stats = {
    "calls": 0,
    "total_k": 0,
    "max_k": 0,
    "cap_hits": 0,
    "pagination_calls": 0,
    "faceted_calls": 0,
    "detail_full": 0,
    "detail_brief": 0,
    "detail_compact": 0,
}
_STATS_LOG_INTERVAL = 50

# Context variable set by auth middleware before tool execution
_mcp_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "_mcp_principal",
    default=None,
)


async def _resolve_principal(token: str) -> Principal | None:
    """Validate a Bearer token (JWT or API key) and return a Principal."""
    if not token.startswith(API_KEY_PREFIXES):
        try:
            payload = decode_token(token)
            if payload.get("type") != "access":
                return None
            return Principal(
                type="user",
                id=uuid.UUID(payload["sub"]),
                role=payload["role"],
            )
        except Exception:
            pass

        # JWT failed — try OAuth access token
        try:
            async with async_session_factory() as db:
                result = await validate_oauth_access_token(db, token)
                if result is not None:
                    await db.commit()  # persist last_used_at update
                    user_id, role = result
                    return Principal(type="oauth", id=user_id, role=role)
        except Exception:
            logger.debug("OAuth token validation failed", exc_info=True)
        return None

    # API key lookup
    key_hash = hash_api_key(token)
    async with async_session_factory() as session:
        result = await session.execute(
            select(ApiKey).where(
                ApiKey.key_hash == key_hash,
                ApiKey.is_active.is_(True),
            )
        )
        api_key = result.scalar_one_or_none()
        if api_key is None:
            return None
        await session.execute(
            update(ApiKey).where(ApiKey.key_id == api_key.key_id).values(last_used_at=datetime.now(UTC))
        )
        await session.commit()
        return Principal(type="api_key", id=api_key.key_id, role="user")


class MCPAuthMiddleware:
    """ASGI middleware that extracts Bearer token and sets _mcp_principal."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Pass through lifespan events untouched so the MCP session manager initializes
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            principal = await _resolve_principal(token)
            if principal is not None:
                reset_token = _mcp_principal.set(principal)
                try:
                    await self.app(scope, receive, send)
                finally:
                    _mcp_principal.reset(reset_token)
                return

        # No valid auth — return 401 JSON
        body = json.dumps({"error": "Unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class MCPTokenPathAuth:
    """ASGI middleware that extracts an API key from the URL path.

    Intended for authless MCP clients (Claude.ai, ChatGPT) that cannot
    send custom Authorization headers.  The user pastes a URL like
    ``https://tunnel.example.com/t/hc_abc123...`` into the client's
    connector settings.  This middleware extracts the key from the first
    path segment, validates it, and rewrites the path before forwarding
    to the inner MCP ASGI app.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        parts = path.strip("/").split("/", 1)
        token = parts[0] if parts else ""

        if not token or not token.startswith(API_KEY_PREFIXES):
            await self._send_401(send)
            return

        principal = await _resolve_principal(token)
        if principal is None:
            await self._send_401(send)
            return

        # Rewrite path: strip the token, keep the rest (if any)
        remaining = "/" + parts[1] if len(parts) > 1 else "/"
        scope = dict(scope)
        scope["path"] = remaining

        reset_token = _mcp_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _mcp_principal.reset(reset_token)

    @staticmethod
    async def _send_401(send):
        body = json.dumps({"error": "Invalid or missing API key in URL path"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _get_principal() -> Principal:
    """Get the current MCP principal or raise."""
    p = _mcp_principal.get()
    if p is None:
        raise PermissionError("Not authenticated")
    return p


def _require_admin() -> Principal:
    p = _get_principal()
    if p.role != "admin":
        raise PermissionError("Admin access required")
    return p


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

mcp = FastMCP(
    "Harbor Clerk",
    # DNS rebinding protection is unnecessary — we run behind Caddy with
    # our own Bearer-token auth middleware wrapping the MCP ASGI app.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def _resolve_headings(
    session: AsyncSession,
    hits: list[SearchHit],
) -> dict[tuple[str, int], str]:
    """Bulk-resolve the nearest heading at or before each hit's page.

    Returns a map of (version_id_str, page_start) → heading title.
    """
    # Collect unique (version_id, page_start) pairs that have pages
    pairs: set[tuple[uuid.UUID, int]] = set()
    for h in hits:
        if h.page_start is not None:
            pairs.add((uuid.UUID(h.version_id), h.page_start))

    if not pairs:
        return {}

    # Load all headings for the relevant versions
    version_ids = list({vid for vid, _ in pairs})
    heading_result = await session.execute(
        select(DocumentHeading)
        .where(DocumentHeading.version_id.in_(version_ids))
        .order_by(DocumentHeading.version_id, DocumentHeading.position)
    )
    headings = heading_result.scalars().all()

    # Group headings by version_id
    headings_by_version: dict[uuid.UUID, list[DocumentHeading]] = {}
    for heading in headings:
        headings_by_version.setdefault(heading.version_id, []).append(heading)

    # For each (version_id, page), find the nearest heading at or before that page
    result_map: dict[tuple[str, int], str] = {}
    for vid, page in pairs:
        version_headings = headings_by_version.get(vid, [])
        best: DocumentHeading | None = None
        for heading in version_headings:
            if heading.page_num is not None and heading.page_num <= page:
                best = heading
            elif heading.page_num is not None and heading.page_num > page:
                break
        if best is not None:
            result_map[(str(vid), page)] = best.title

    return result_map


def _format_search_response(
    result: SearchResult,
    detail: str,
    effective_brief_chars: int,
    k: int,
    offset: int,
    heading_map: dict[tuple[str, int], str] | None = None,
) -> dict:
    """Build the non-faceted search response dict from a SearchResult.

    Shared between kb_search and kb_batch_search.
    """
    hits = []
    for h in result.hits:
        hit: dict = {
            "chunk_id": h.chunk_id,
            "doc_id": h.doc_id,
            "doc_title": h.doc_title,
            "version_id": h.version_id,
            "score": h.score,
            "language": h.language,
        }
        if h.page_start is not None:
            hit["pages"] = f"{h.page_start}-{h.page_end}" if h.page_end != h.page_start else str(h.page_start)
            # Include nearest heading for document context
            if heading_map:
                heading = heading_map.get((h.version_id, h.page_start))
                if heading:
                    hit["section"] = heading
        # Detail mode formatting
        if detail == "full":
            hit["text"] = h.chunk_text
        elif detail == "brief":
            text = h.chunk_text
            if len(text) > effective_brief_chars:
                hit["text"] = text[:effective_brief_chars] + "\u2026"
            else:
                hit["text"] = text
        # compact: no text field
        hits.append(hit)

    has_more = offset + k < result.total_candidates

    resp: dict = {
        "hits": hits,
        "total_candidates": result.total_candidates,
        "has_more": has_more,
    }
    if result.possible_conflict:
        resp["possible_conflict"] = True
        resp["conflict_sources"] = [
            {"doc_id": cs.doc_id, "version_id": cs.version_id, "title": cs.title} for cs in result.conflict_sources
        ]
    return resp


@mcp.tool()
async def kb_search(
    query: str,
    k: int = 10,
    offset: int = 0,
    detail: str = "full",
    brief_chars: int = 0,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    language: str | None = None,
    mime_type: str | None = None,
    faceted: bool = False,
) -> str:
    """Search the knowledge base with hybrid FTS + vector search.

    Returns ranked chunks with citations (page numbers, scores, section
    headings). Each hit includes the nearest document heading above the
    chunk's page (in the "section" field), so you can see where in the
    document the passage comes from without a separate outline call.

    Use this as the primary tool to find information.

    Filters (all optional):
      doc_id: restrict to a single document (mutually exclusive with doc_ids)
      doc_ids: restrict to multiple documents (list of UUIDs, max 50)
      after: only versions created at or after this ISO datetime
      before: only versions created before this ISO datetime
      language: chunk language filter ("english" or "french")
      mime_type: version MIME type filter (e.g. "application/pdf")

    detail levels control how much text is returned per hit:
      "full" (default): complete chunk text — best for reading a small
        number of high-confidence results carefully
      "brief": first ~200 characters per chunk (adjustable via brief_chars) —
        use when scanning 20-50 results to identify which are worth
        reading in full via kb_read_passages
      "compact": metadata only (chunk_id, doc_id, version_id, doc_title,
        score, pages, language — no text) — use when surveying a broad result set (50+) to
        understand score distribution and document coverage before
        narrowing down

    faceted: if true, groups hits by document with per-document top_score
      and hit_count — useful for understanding which documents are most
      relevant at a glance

    Pagination: use offset to page through results. Check has_more
    in the response to know if more results exist beyond your window.
    """
    _get_principal()
    settings = get_settings()

    # Mutual exclusion check
    if doc_id is not None and doc_ids is not None:
        return json.dumps({"error": "Cannot specify both doc_id and doc_ids"})

    did = uuid.UUID(doc_id) if doc_id else None

    # Parse doc_ids
    parsed_doc_ids = None
    if doc_ids is not None:
        if len(doc_ids) > 50:
            return json.dumps({"error": "doc_ids limited to 50 entries"})
        try:
            parsed_doc_ids = [uuid.UUID(d) for d in doc_ids]
        except ValueError:
            return json.dumps({"error": "Invalid UUID in doc_ids"})

    # Parse dates
    parsed_after = None
    parsed_before = None
    if after is not None:
        try:
            parsed_after = datetime.fromisoformat(after)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for after: {after}"})
    if before is not None:
        try:
            parsed_before = datetime.fromisoformat(before)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for before: {before}"})

    # Clamp parameters
    k = max(1, min(k, settings.mcp_max_k))
    offset = max(0, offset)
    if detail not in ("full", "brief", "compact"):
        detail = "full"

    async with async_session_factory() as session:
        result = await hybrid_search(
            session,
            query,
            k=k,
            doc_id=did,
            offset=offset,
            doc_ids=parsed_doc_ids,
            after=parsed_after,
            before=parsed_before,
            language=language,
            mime_type=mime_type,
        )
        heading_map = await _resolve_headings(session, result.hits)

    # Resolve brief_chars for brief mode
    effective_brief_chars = brief_chars if brief_chars > 0 else settings.mcp_brief_chars

    base_resp = _format_search_response(result, detail, effective_brief_chars, k, offset, heading_map)

    if faceted:
        # Group hits by doc_id
        groups: dict[str, list[dict]] = {}
        for h in base_resp["hits"]:
            groups.setdefault(h["doc_id"], []).append(h)
        doc_groups = []
        for did_str, group_hits in groups.items():
            doc_groups.append(
                {
                    "doc_id": did_str,
                    "doc_title": group_hits[0].get("doc_title"),
                    "top_score": max(h["score"] for h in group_hits),
                    "hit_count": len(group_hits),
                    "hits": group_hits,
                }
            )
        doc_groups.sort(key=lambda g: g["top_score"], reverse=True)
        resp: dict = {
            "documents": doc_groups,
            "total_candidates": result.total_candidates,
            "has_more": base_resp["has_more"],
        }
    else:
        resp = base_resp

    # Runtime stats
    _search_stats["calls"] += 1
    _search_stats["total_k"] += k
    _search_stats["max_k"] = max(_search_stats["max_k"], k)
    if k >= settings.mcp_max_k:
        _search_stats["cap_hits"] += 1
    if offset > 0:
        _search_stats["pagination_calls"] += 1
    if faceted:
        _search_stats["faceted_calls"] += 1
    _search_stats[f"detail_{detail}"] += 1

    if _search_stats["calls"] % _STATS_LOG_INTERVAL == 0:
        n = _search_stats["calls"]
        logger.info(
            "kb_search stats (%d calls): avg_k=%.0f, max_k=%d, cap_hit_rate=%.0f%%, "
            "pagination_rate=%.0f%%, faceted_rate=%.0f%%, detail: full=%d brief=%d compact=%d",
            n,
            _search_stats["total_k"] / n,
            _search_stats["max_k"],
            100 * _search_stats["cap_hits"] / n,
            100 * _search_stats["pagination_calls"] / n,
            100 * _search_stats["faceted_calls"] / n,
            _search_stats["detail_full"],
            _search_stats["detail_brief"],
            _search_stats["detail_compact"],
        )

    return json.dumps(resp, indent=2)


@mcp.tool()
async def kb_batch_search(
    queries: list[str],
    k: int = 10,
    detail: str = "full",
    brief_chars: int = 0,
    doc_id: str | None = None,
    doc_ids: list[str] | None = None,
    after: str | None = None,
    before: str | None = None,
    language: str | None = None,
    mime_type: str | None = None,
) -> str:
    """Run multiple search queries in one call (max 5).

    Returns results grouped by query — useful for comparative analysis
    ("do any of these case files mention X, Y, or Z?") without
    sequential round-trips.

    All filters are shared across queries. Each query gets its own
    result set with hits, total_candidates, and has_more.

    See kb_search for filter and detail level documentation.
    """
    _get_principal()
    settings = get_settings()

    # Validate query count
    if not queries:
        return json.dumps({"error": "queries must contain at least 1 query"})
    if len(queries) > 5:
        return json.dumps({"error": "queries limited to max 5 per call"})

    # Mutual exclusion check
    if doc_id is not None and doc_ids is not None:
        return json.dumps({"error": "Cannot specify both doc_id and doc_ids"})

    try:
        did = uuid.UUID(doc_id) if doc_id else None
    except ValueError:
        return json.dumps({"error": f"Invalid UUID for doc_id: {doc_id}"})

    # Parse doc_ids
    parsed_doc_ids = None
    if doc_ids is not None:
        if len(doc_ids) > 50:
            return json.dumps({"error": "doc_ids limited to 50 entries"})
        try:
            parsed_doc_ids = [uuid.UUID(d) for d in doc_ids]
        except ValueError:
            return json.dumps({"error": "Invalid UUID in doc_ids"})

    # Parse dates
    parsed_after = None
    parsed_before = None
    if after is not None:
        try:
            parsed_after = datetime.fromisoformat(after)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for after: {after}"})
    if before is not None:
        try:
            parsed_before = datetime.fromisoformat(before)
        except ValueError:
            return json.dumps({"error": f"Invalid ISO datetime for before: {before}"})

    # Clamp parameters
    k = max(1, min(k, settings.mcp_max_k))
    if detail not in ("full", "brief", "compact"):
        detail = "full"

    effective_brief_chars = brief_chars if brief_chars > 0 else settings.mcp_brief_chars

    results = []
    async with async_session_factory() as session:
        for query in queries:
            result = await hybrid_search(
                session,
                query,
                k=k,
                doc_id=did,
                offset=0,
                doc_ids=parsed_doc_ids,
                after=parsed_after,
                before=parsed_before,
                language=language,
                mime_type=mime_type,
            )
            heading_map = await _resolve_headings(session, result.hits)
            resp = _format_search_response(result, detail, effective_brief_chars, k, 0, heading_map)
            resp["query"] = query
            results.append(resp)

            # Runtime stats per query
            _search_stats["calls"] += 1
            _search_stats["total_k"] += k
            _search_stats["max_k"] = max(_search_stats["max_k"], k)
            if k >= settings.mcp_max_k:
                _search_stats["cap_hits"] += 1
            _search_stats[f"detail_{detail}"] += 1

    if _search_stats["calls"] % _STATS_LOG_INTERVAL == 0 and _search_stats["calls"] > 0:
        n = _search_stats["calls"]
        logger.info(
            "kb_search stats (%d calls): avg_k=%.0f, max_k=%d, cap_hit_rate=%.0f%%, "
            "detail: full=%d brief=%d compact=%d",
            n,
            _search_stats["total_k"] / n,
            _search_stats["max_k"],
            100 * _search_stats["cap_hits"] / n,
            _search_stats["detail_full"],
            _search_stats["detail_brief"],
            _search_stats["detail_compact"],
        )

    return json.dumps({"results": results}, indent=2)


@mcp.tool()
async def kb_read_passages(
    chunk_ids: list[str],
    include_context: bool = False,
) -> str:
    """Read full text of specific passages by their chunk IDs.

    Use after kb_search (especially with detail="brief" or "compact")
    to fetch the complete text of interesting results. Returns each
    passage with its document title, language, and page numbers.

    Set include_context=True to also get the immediately preceding and
    following chunks — useful for understanding a passage in context
    without a separate kb_expand_context call.
    """
    _get_principal()
    uuids = [uuid.UUID(cid) for cid in chunk_ids]

    async with async_session_factory() as session:
        result = await session.execute(select(Chunk).where(Chunk.chunk_id.in_(uuids)))
        chunks = {c.chunk_id: c for c in result.scalars().all()}

        # Doc titles
        doc_ids = {c.doc_id for c in chunks.values()}
        docs_result = await session.execute(select(Document).where(Document.doc_id.in_(list(doc_ids))))
        docs = {d.doc_id: d for d in docs_result.scalars().all()}

        passages = []
        for cid in uuids:
            chunk = chunks.get(cid)
            if chunk is None:
                continue
            doc = docs.get(chunk.doc_id)
            p: dict = {
                "chunk_id": str(cid),
                "doc_title": doc.title if doc else None,
                "text": chunk.chunk_text,
                "language": chunk.language,
            }
            if chunk.page_start is not None:
                p["pages"] = (
                    f"{chunk.page_start}-{chunk.page_end}"
                    if chunk.page_end != chunk.page_start
                    else str(chunk.page_start)
                )

            if include_context:
                prev = await session.execute(
                    select(Chunk.chunk_text).where(
                        Chunk.version_id == chunk.version_id,
                        Chunk.chunk_num == chunk.chunk_num - 1,
                    )
                )
                prev_text = prev.scalar_one_or_none()
                if prev_text:
                    p["context_before"] = prev_text

                nxt = await session.execute(
                    select(Chunk.chunk_text).where(
                        Chunk.version_id == chunk.version_id,
                        Chunk.chunk_num == chunk.chunk_num + 1,
                    )
                )
                nxt_text = nxt.scalar_one_or_none()
                if nxt_text:
                    p["context_after"] = nxt_text

            passages.append(p)

    return json.dumps({"passages": passages}, indent=2)


@mcp.tool()
async def kb_expand_context(chunk_id: str, n: int = 2) -> str:
    """Expand context around a chunk — returns the target plus up to N chunks
    before and after from the same document version, in order.

    Use after kb_search or kb_read_passages when you need more surrounding
    text. The target chunk is marked with "is_target": true.
    """
    _get_principal()
    n = max(1, min(n, 10))
    target_id = uuid.UUID(chunk_id)

    async with async_session_factory() as session:
        target = (await session.execute(select(Chunk).where(Chunk.chunk_id == target_id))).scalar_one_or_none()
        if target is None:
            return json.dumps({"error": "Chunk not found"})

        result = await session.execute(
            select(Chunk)
            .where(
                Chunk.version_id == target.version_id,
                Chunk.chunk_num.between(target.chunk_num - n, target.chunk_num + n),
            )
            .order_by(Chunk.chunk_num)
        )
        neighbours = result.scalars().all()

        doc = (await session.execute(select(Document).where(Document.doc_id == target.doc_id))).scalar_one_or_none()

        chunks = []
        for c in neighbours:
            entry: dict = {
                "chunk_id": str(c.chunk_id),
                "chunk_num": c.chunk_num,
                "text": c.chunk_text,
                "language": c.language,
            }
            if c.page_start is not None:
                entry["pages"] = f"{c.page_start}-{c.page_end}" if c.page_end != c.page_start else str(c.page_start)
            if c.chunk_id == target_id:
                entry["is_target"] = True
            chunks.append(entry)

    return json.dumps(
        {
            "doc_id": str(target.doc_id),
            "doc_title": doc.title if doc else None,
            "version_id": str(target.version_id),
            "target_chunk_num": target.chunk_num,
            "chunks": chunks,
        },
        indent=2,
    )


@mcp.tool()
async def kb_get_document(doc_id: str) -> str:
    """Get full metadata for a document: title, status, and all versions.

    Each version includes its processing status, summary, MIME type,
    file size, extracted character count, and ingestion pipeline jobs.
    Use this to inspect a specific document after finding it via search
    or kb_list_recent.
    """
    _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Document).options(selectinload(Document.versions)).where(Document.doc_id == did)
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        versions = []
        for v in doc.versions or []:
            jobs_result = await session.execute(select(IngestionJob).where(IngestionJob.version_id == v.version_id))
            jobs = [
                {"stage": j.stage.value, "status": j.status.value, "error": j.error}
                for j in jobs_result.scalars().all()
            ]
            versions.append(
                {
                    "version_id": str(v.version_id),
                    "status": v.status.value,
                    "summary": v.summary,
                    "mime_type": v.mime_type,
                    "size_bytes": v.size_bytes,
                    "extracted_chars": v.extracted_chars,
                    "source_path": v.source_path,
                    "created_at": v.created_at.isoformat(),
                    "jobs": jobs,
                }
            )

    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "title": doc.title,
            "status": doc.status,
            "latest_version_id": str(doc.latest_version_id) if doc.latest_version_id else None,
            "versions": versions,
        },
        indent=2,
    )


@mcp.tool()
async def kb_list_recent(limit: int = 20) -> str:
    """List documents ordered by most recently updated, with summaries.

    Returns title, summary, status, version count, and update timestamp
    for each document. Use this to see what's new or recently changed,
    or as a paginated document browser (max 100 per call).
    """
    _get_principal()

    async with async_session_factory() as session:
        total_result = await session.execute(
            select(func.count()).select_from(Document).where(Document.status == "active")
        )
        total_count = total_result.scalar() or 0

        result = await session.execute(
            select(Document)
            .options(selectinload(Document.versions))
            .where(Document.status == "active")
            .order_by(Document.updated_at.desc())
            .limit(min(limit, 100))
        )
        docs = result.scalars().all()

    items = []
    for doc in docs:
        latest_status = None
        latest_summary = None
        if doc.latest_version_id and doc.versions:
            for v in doc.versions:
                if v.version_id == doc.latest_version_id:
                    latest_status = v.status.value
                    latest_summary = v.summary
                    break
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "summary": latest_summary,
                "status": doc.status,
                "latest_version_status": latest_status,
                "version_count": len(doc.versions) if doc.versions else 0,
                "updated_at": doc.updated_at.isoformat(),
            }
        )

    return json.dumps(
        {"total_count": total_count, "truncated": total_count > len(items), "documents": items},
        indent=2,
    )


@mcp.tool()
async def kb_corpus_overview(limit: int = 50) -> str:
    """Get a bird's-eye view of the knowledge base.

    Returns corpus-level statistics: document count, chunk/page totals,
    language distribution, file type breakdown, date range, and a list
    of documents with titles and summaries.

    Use this as your first call when exploring an unfamiliar corpus.
    The statistics section is always complete; only the document list
    is subject to the limit.

    Args:
        limit: Maximum number of documents to include in the list
            (default 50, max 500). Increase if you need to scan more
            documents; decrease for faster responses with large corpora.
            The response includes a 'truncated' flag and total
            'document_count' so you know if more exist."""
    _get_principal()

    limit = max(1, min(limit, 500))

    async with async_session_factory() as session:
        doc_count_result = await session.execute(
            select(func.count()).select_from(Document).where(Document.status == "active")
        )
        doc_count = doc_count_result.scalar() or 0

        chunk_count_result = await session.execute(
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.latest_version_id == Chunk.version_id)
            .where(Document.status == "active")
        )
        chunk_count = chunk_count_result.scalar() or 0

        page_count_result = await session.execute(
            select(func.count())
            .select_from(DocumentPage)
            .join(Document, Document.latest_version_id == DocumentPage.version_id)
            .where(Document.status == "active")
        )
        total_pages = page_count_result.scalar() or 0

        # Language distribution from chunks (scoped to active docs' latest versions)
        lang_rows = (
            await session.execute(
                select(Chunk.language, func.count())
                .join(Document, Document.latest_version_id == Chunk.version_id)
                .where(Document.status == "active")
                .group_by(Chunk.language)
                .order_by(func.count().desc())
            )
        ).all()
        languages = {row[0]: row[1] for row in lang_rows if row[0]}

        # Mime type breakdown from latest versions of active docs
        mime_rows = (
            await session.execute(
                select(DocumentVersion.mime_type, func.count())
                .join(Document, Document.latest_version_id == DocumentVersion.version_id)
                .where(Document.status == "active")
                .group_by(DocumentVersion.mime_type)
                .order_by(func.count().desc())
            )
        ).all()
        mime_types = {row[0]: row[1] for row in mime_rows if row[0]}

        # Date range
        date_result = await session.execute(
            select(func.min(Document.updated_at), func.max(Document.updated_at)).where(Document.status == "active")
        )
        date_row = date_result.one()
        oldest = date_row[0].isoformat() if date_row[0] else None
        newest = date_row[1].isoformat() if date_row[1] else None

        result = await session.execute(
            select(Document)
            .options(selectinload(Document.versions))
            .where(Document.status == "active")
            .order_by(Document.updated_at.desc())
            .limit(limit)
        )
        docs = result.scalars().all()

    items = []
    for doc in docs:
        summary = None
        status = None
        if doc.latest_version_id and doc.versions:
            for v in doc.versions:
                if v.version_id == doc.latest_version_id:
                    summary = v.summary
                    status = v.status.value
                    break
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "summary": summary,
                "status": status,
                "updated_at": doc.updated_at.isoformat(),
            }
        )

    return json.dumps(
        {
            "document_count": doc_count,
            "total_chunks": chunk_count,
            "total_pages": total_pages,
            "languages": languages,
            "mime_types": mime_types,
            "date_range": {"oldest": oldest, "newest": newest},
            "documents": items,
            "truncated": doc_count > len(items),
        },
        indent=2,
    )


@mcp.tool()
async def kb_ingest_status(doc_id: str) -> str:
    """Check ingestion pipeline progress for a document's latest version.

    Returns the status of each pipeline stage (extract → ocr → chunk →
    entities → embed → summarize → finalize) with progress counts,
    timestamps, and any error messages. Use this to check if a document
    is still being processed or to diagnose ingestion failures.
    """
    _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(select(Document).where(Document.doc_id == did))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        vid = doc.latest_version_id
        if vid is None and doc.versions:
            vid = doc.versions[-1].version_id
        if vid is None:
            return json.dumps({"error": "No versions"})

        ver_result = await session.execute(select(DocumentVersion).where(DocumentVersion.version_id == vid))
        version = ver_result.scalar_one()

        jobs_result = await session.execute(
            select(IngestionJob).where(IngestionJob.version_id == vid).order_by(IngestionJob.created_at)
        )
        jobs = [
            {
                "stage": j.stage.value,
                "status": j.status.value,
                "progress": f"{j.progress_current}/{j.progress_total}" if j.progress_total else None,
                "error": j.error,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
            }
            for j in jobs_result.scalars().all()
        ]

    return json.dumps(
        {
            "doc_id": str(did),
            "version_id": str(vid),
            "version_status": version.status.value,
            "jobs": jobs,
        },
        indent=2,
    )


@mcp.tool()
async def kb_reprocess(doc_id: str) -> str:
    """Re-run the full ingestion pipeline for a document (admin only).

    Resets all pipeline stages and re-queues from the beginning.
    Use when a document's content appears stale or ingestion failed
    and you want to retry from scratch.
    """
    _require_admin()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        vid = doc.latest_version_id
        if vid is None and doc.versions:
            vid = doc.versions[-1].version_id
        if vid is None:
            return json.dumps({"error": "No version to reprocess"})

        ver_result = await session.execute(select(DocumentVersion).where(DocumentVersion.version_id == vid))
        version = ver_result.scalar_one()
        version.status = VersionStatus.queued
        version.error = None
        await session.commit()

    from harbor_clerk.worker.pipeline import enqueue_stage, reset_jobs

    reset_jobs(vid)
    enqueue_stage(vid, JobStage.extract)

    return json.dumps(
        {
            "doc_id": str(did),
            "version_id": str(vid),
            "status": "reprocessing",
        }
    )


@mcp.tool()
async def kb_document_outline(doc_id: str) -> str:
    """Get the heading outline/structure of a document, including page and chunk counts.

    Returns the heading hierarchy (h1-h6), total page count, and total chunk count
    for the latest version of the document. Useful for understanding document structure
    before reading specific sections.
    """
    _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Document)
            .options(selectinload(Document.versions))
            .where(Document.doc_id == did, Document.status == "active")
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        vid = doc.latest_version_id
        if vid is None and doc.versions:
            vid = doc.versions[-1].version_id
        if vid is None:
            return json.dumps({"error": "No versions available"})

        headings_result = await session.execute(
            select(DocumentHeading).where(DocumentHeading.version_id == vid).order_by(DocumentHeading.position)
        )
        headings = headings_result.scalars().all()

        page_count = (
            await session.execute(select(func.count()).select_from(DocumentPage).where(DocumentPage.version_id == vid))
        ).scalar_one()

        chunk_count = (
            await session.execute(select(func.count()).select_from(Chunk).where(Chunk.version_id == vid))
        ).scalar_one()

    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "version_id": str(vid),
            "title": doc.title,
            "page_count": page_count,
            "chunk_count": chunk_count,
            "headings": [
                {
                    "level": h.level,
                    "title": h.title,
                    "page_num": h.page_num,
                }
                for h in headings
            ],
        },
        indent=2,
    )


@mcp.tool()
async def kb_find_related(doc_id: str, k: int = 5) -> str:
    """Find documents most similar to a given document based on embedding similarity.

    Computes the average embedding of the document's chunks and finds the
    closest documents by cosine distance. Useful for discovering related
    content, finding duplicates, or understanding topic clusters.

    Args:
        doc_id: The document to find related documents for.
        k: Number of related documents to return (1-20, default 5).
    """
    _get_principal()
    k = max(1, min(k, 20))

    try:
        target_id = uuid.UUID(doc_id)
    except ValueError:
        return json.dumps({"error": f"Invalid doc_id: {doc_id}"})

    async with async_session_factory() as session:
        # Verify document exists and get latest version
        doc = (
            await session.execute(select(Document).where(Document.doc_id == target_id, Document.status == "active"))
        ).scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        version_id = doc.latest_version_id
        if version_id is None:
            return json.dumps({"error": "Document has no versions"})

        # Get all embeddings for this document's latest version
        rows = (
            await session.execute(
                select(Chunk.embedding).where(
                    Chunk.version_id == version_id,
                    Chunk.embedding.isnot(None),
                )
            )
        ).all()

        if not rows:
            return json.dumps({"doc_id": doc_id, "related": [], "note": "No embeddings available"})

        # Compute average embedding in Python
        dim = len(rows[0][0])
        avg = [0.0] * dim
        for (emb,) in rows:
            for i, v in enumerate(emb):
                avg[i] += v
        n = len(rows)
        avg = [v / n for v in avg]

        # Find nearest chunks from OTHER active documents' latest versions
        distance = Chunk.embedding.cosine_distance(avg)
        nearest = (
            await session.execute(
                select(
                    Chunk.doc_id,
                    func.min(distance).label("min_distance"),
                )
                .join(Document, Document.latest_version_id == Chunk.version_id)
                .where(
                    Document.status == "active",
                    Chunk.embedding.isnot(None),
                    Chunk.doc_id != target_id,
                )
                .group_by(Chunk.doc_id)
                .order_by(func.min(distance))
                .limit(k)
            )
        ).all()

        if not nearest:
            return json.dumps({"doc_id": doc_id, "related": []})

        # Fetch document metadata for results
        related_ids = [row[0] for row in nearest]
        distances = {row[0]: float(row[1]) for row in nearest}

        docs_result = await session.execute(
            select(Document).options(selectinload(Document.versions)).where(Document.doc_id.in_(related_ids))
        )
        related_docs = {d.doc_id: d for d in docs_result.scalars().all()}

    items = []
    for rid in related_ids:
        rdoc = related_docs.get(rid)
        if not rdoc:
            continue
        summary = None
        if rdoc.latest_version_id and rdoc.versions:
            for v in rdoc.versions:
                if v.version_id == rdoc.latest_version_id:
                    summary = v.summary
                    break
        items.append(
            {
                "doc_id": str(rid),
                "title": rdoc.title,
                "summary": summary,
                "similarity": round(1.0 - distances[rid], 4),
            }
        )

    return json.dumps(
        {"doc_id": doc_id, "related": items},
        indent=2,
    )


@mcp.tool()
async def kb_entity_search(
    query: str,
    entity_type: str | None = None,
    doc_id: str | None = None,
    deduplicate: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Search for named entities (people, organizations, places, etc.) in the corpus.

    Args:
        query: Substring search on entity text (case-insensitive).
        entity_type: Filter by entity type (PERSON, ORG, GPE, LOC, DATE, etc.).
        doc_id: Scope to a single document's latest version.
        deduplicate: If true, group by entity_text+entity_type and return mention counts.
        limit: Max results (1-100, default 20).
        offset: Pagination offset.
    """
    _get_principal()
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    # Escape ILIKE metacharacters in query
    escaped_query = re.sub(r"([%_\\])", r"\\\1", query)

    async with async_session_factory() as session:
        # Base filter: active docs, latest version
        base_filter = [
            Entity.entity_text.ilike(f"%{escaped_query}%"),
        ]

        if entity_type:
            base_filter.append(Entity.entity_type == entity_type)

        if doc_id:
            did = uuid.UUID(doc_id)
            # Scope to latest version of the document
            doc = (
                await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
            ).scalar_one_or_none()
            if doc is None:
                return json.dumps({"error": "Document not found"})
            if doc.latest_version_id:
                base_filter.append(Entity.version_id == doc.latest_version_id)
            else:
                return json.dumps({"entities": [], "total": 0, "has_more": False})
        else:
            # Scope to active docs' latest versions
            base_filter.append(
                Entity.version_id.in_(
                    select(Document.latest_version_id).where(
                        Document.status == "active",
                        Document.latest_version_id.isnot(None),
                    )
                )
            )

        if deduplicate:
            count_q = (
                select(
                    Entity.entity_text,
                    Entity.entity_type,
                    func.count().label("mention_count"),
                )
                .where(*base_filter)
                .group_by(Entity.entity_text, Entity.entity_type)
            )
            # Total
            total_q = select(func.count()).select_from(count_q.subquery())
            total = (await session.execute(total_q)).scalar() or 0

            rows = (await session.execute(count_q.order_by(func.count().desc()).offset(offset).limit(limit))).all()
            entities = [
                {
                    "entity_text": r[0],
                    "entity_type": r[1],
                    "mention_count": r[2],
                }
                for r in rows
            ]
        else:
            # Total count
            total = (await session.execute(select(func.count()).select_from(Entity).where(*base_filter))).scalar() or 0

            rows = (
                (
                    await session.execute(
                        select(Entity).where(*base_filter).order_by(Entity.entity_text).offset(offset).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            entities = [
                {
                    "entity_id": str(e.entity_id),
                    "entity_text": e.entity_text,
                    "entity_type": e.entity_type,
                    "doc_id": str(e.doc_id),
                    "chunk_id": str(e.chunk_id),
                    "start_char": e.start_char,
                    "end_char": e.end_char,
                }
                for e in rows
            ]

    return json.dumps(
        {
            "entities": entities,
            "total": total,
            "has_more": offset + limit < total,
        },
        indent=2,
    )


@mcp.tool()
async def kb_entity_overview(doc_id: str | None = None) -> str:
    """Get an overview of named entities in the corpus or a specific document.

    Returns entity type distribution, total/unique counts, and top entities
    by mention frequency. Useful for understanding what people, organizations,
    and places appear in the knowledge base.

    Args:
        doc_id: Optional — scope to a single document's latest version.
    """
    _get_principal()

    async with async_session_factory() as session:
        # Build version filter
        if doc_id:
            did = uuid.UUID(doc_id)
            doc = (
                await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
            ).scalar_one_or_none()
            if doc is None:
                return json.dumps({"error": "Document not found"})
            if not doc.latest_version_id:
                return json.dumps(
                    {
                        "total_entities": 0,
                        "unique_entities": 0,
                        "type_distribution": {},
                        "top_entities": [],
                    }
                )
            version_filter = [Entity.version_id == doc.latest_version_id]
        else:
            version_filter = [
                Entity.version_id.in_(
                    select(Document.latest_version_id).where(
                        Document.status == "active",
                        Document.latest_version_id.isnot(None),
                    )
                )
            ]

        # Total entities
        total = (await session.execute(select(func.count()).select_from(Entity).where(*version_filter))).scalar() or 0

        # Unique entities
        unique_q = select(Entity.entity_text, Entity.entity_type).where(*version_filter).distinct()
        unique = (await session.execute(select(func.count()).select_from(unique_q.subquery()))).scalar() or 0

        # Type distribution
        type_rows = (
            await session.execute(
                select(Entity.entity_type, func.count())
                .where(*version_filter)
                .group_by(Entity.entity_type)
                .order_by(func.count().desc())
            )
        ).all()
        type_distribution = {row[0]: row[1] for row in type_rows}

        # Top entities by mention count (deduplicated)
        top_rows = (
            await session.execute(
                select(
                    Entity.entity_text,
                    Entity.entity_type,
                    func.count().label("mention_count"),
                )
                .where(*version_filter)
                .group_by(Entity.entity_text, Entity.entity_type)
                .order_by(func.count().desc())
                .limit(20)
            )
        ).all()
        top_entities = [
            {
                "entity_text": r[0],
                "entity_type": r[1],
                "mention_count": r[2],
            }
            for r in top_rows
        ]

    return json.dumps(
        {
            "total_entities": total,
            "unique_entities": unique,
            "type_distribution": type_distribution,
            "top_entities": top_entities,
        },
        indent=2,
    )


@mcp.tool()
async def kb_entity_cooccurrence(
    entity_text: str,
    entity_type: str | None = None,
    scope: str = "chunk",
    cooccur_type: str | None = None,
    doc_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Find entities that co-occur with a given entity.

    Discovers relationships by finding which other entities appear alongside
    the specified entity in the same chunk or document.

    Args:
        entity_text: The entity text to find co-occurrences for (case-insensitive).
        entity_type: Optional — filter the source entity by type (PERSON, ORG, etc.).
        scope: "chunk" (same chunk) or "document" (same document version). Default "chunk".
        cooccur_type: Optional — filter co-occurring entities by type.
        doc_id: Optional — scope to a single document's latest version.
        limit: Max results (1-100, default 20).
        offset: Pagination offset.
    """
    _get_principal()

    if scope not in ("chunk", "document"):
        return json.dumps({"error": f"Invalid scope '{scope}'. Must be 'chunk' or 'document'."})

    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    escaped_text = re.sub(r"([%_\\])", r"\\\1", entity_text)

    e1 = aliased(Entity, name="e1")
    e2 = aliased(Entity, name="e2")

    async with async_session_factory() as session:
        # Version filter: active docs' latest versions
        if doc_id:
            did = uuid.UUID(doc_id)
            doc = (
                await session.execute(select(Document).where(Document.doc_id == did, Document.status == "active"))
            ).scalar_one_or_none()
            if doc is None:
                return json.dumps({"error": "Document not found"})
            if not doc.latest_version_id:
                return json.dumps(
                    {"entity_text": entity_text, "scope": scope, "cooccurrences": [], "total": 0, "has_more": False}
                )
            version_filter_e1 = [e1.version_id == doc.latest_version_id]
            version_filter_e2 = [e2.version_id == doc.latest_version_id]
        else:
            active_versions = select(Document.latest_version_id).where(
                Document.status == "active",
                Document.latest_version_id.isnot(None),
            )
            version_filter_e1 = [e1.version_id.in_(active_versions)]
            version_filter_e2 = [e2.version_id.in_(active_versions)]

        # Source entity filter
        source_filter = [e1.entity_text.ilike(f"%{escaped_text}%"), *version_filter_e1]
        if entity_type:
            source_filter.append(e1.entity_type == entity_type)

        # Join condition based on scope
        if scope == "chunk":
            join_cond = e2.chunk_id == e1.chunk_id
        else:
            join_cond = e2.version_id == e1.version_id

        # Co-occurring entity filters
        cooccur_filter = [*version_filter_e2]
        if cooccur_type:
            cooccur_filter.append(e2.entity_type == cooccur_type)

        # Exclude self-matches
        self_exclude = ~(
            (func.lower(e2.entity_text) == func.lower(e1.entity_text)) & (e2.entity_type == e1.entity_type)
        )

        # Build the query
        base_q = (
            select(
                e2.entity_text,
                e2.entity_type,
                func.count().label("cooccurrence_count"),
                func.array_agg(func.distinct(e2.chunk_id)).label("sample_chunk_ids"),
                func.array_agg(func.distinct(e2.doc_id)).label("sample_doc_ids"),
            )
            .join(e1, join_cond)
            .where(*source_filter, *cooccur_filter, self_exclude)
            .group_by(e2.entity_text, e2.entity_type)
        )

        # Total count
        total = (await session.execute(select(func.count()).select_from(base_q.subquery()))).scalar() or 0

        # Paginated results
        rows = (
            await session.execute(base_q.order_by(func.count().desc(), e2.entity_text).offset(offset).limit(limit))
        ).all()

        cooccurrences = [
            {
                "entity_text": r.entity_text,
                "entity_type": r.entity_type,
                "cooccurrence_count": r.cooccurrence_count,
                "sample_chunk_ids": [str(cid) for cid in r.sample_chunk_ids[:3]],
                "sample_doc_ids": [str(did) for did in list(dict.fromkeys(r.sample_doc_ids))[:3]],
            }
            for r in rows
        ]

    return json.dumps(
        {
            "entity_text": entity_text,
            "scope": scope,
            "cooccurrences": cooccurrences,
            "total": total,
            "has_more": offset + limit < total,
        },
        indent=2,
    )


@mcp.tool()
async def kb_read_document(
    doc_id: str,
    page_start: int | None = None,
    page_end: int | None = None,
    max_chars: int = 50000,
) -> str:
    """Read a document's text by page range.

    CAUTION: Full documents can be very large. Prefer kb_search +
    kb_read_passages for targeted retrieval. Use this only for specific
    page ranges after checking kb_document_outline.

    Returns page-level text from DocumentPage rows for the latest version.
    If no pages exist (e.g. plain-text files), falls back to concatenated chunks.
    Use page_start/page_end to limit the range. max_chars caps total output
    (default 50 000, max 100 000).
    """
    _get_principal()
    try:
        did = uuid.UUID(doc_id)
    except ValueError:
        return json.dumps({"error": f"Invalid doc_id: {doc_id}"})
    max_chars = max(1, min(max_chars, 100_000))

    async with async_session_factory() as session:
        result = await session.execute(
            select(Document)
            .options(selectinload(Document.versions))
            .where(Document.doc_id == did, Document.status == "active")
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        vid = doc.latest_version_id
        if vid is None and doc.versions:
            vid = doc.versions[-1].version_id
        if vid is None:
            return json.dumps({"error": "No versions available"})

        # Total page count
        page_count = (
            await session.execute(select(func.count()).select_from(DocumentPage).where(DocumentPage.version_id == vid))
        ).scalar_one()

        if page_count == 0:
            # Fallback: concatenate chunks (page_start/page_end not applicable)
            chunk_rows = (
                (await session.execute(select(Chunk).where(Chunk.version_id == vid).order_by(Chunk.chunk_num)))
                .scalars()
                .all()
            )

            note = None
            if page_start is not None or page_end is not None:
                note = "page_start/page_end ignored: document has no page records, returning chunk text"

            pages_out: list[dict] = []
            total_chars = 0
            truncated = False
            for c in chunk_rows:
                text = c.chunk_text
                if total_chars + len(text) > max_chars:
                    text = text[: max_chars - total_chars]
                    pages_out.append({"chunk_num": c.chunk_num, "text": text})
                    total_chars += len(text)
                    truncated = True
                    break
                pages_out.append({"chunk_num": c.chunk_num, "text": text})
                total_chars += len(text)

            resp: dict = {
                "doc_id": str(doc.doc_id),
                "version_id": str(vid),
                "title": doc.title,
                "source": "chunks",
                "page_count": 0,
                "pages_returned": len(pages_out),
                "total_chars": total_chars,
                "truncated": truncated,
                "pages": pages_out,
            }
            if note:
                resp["note"] = note
            return json.dumps(resp, indent=2)

        # Query pages with optional range filter
        query = select(DocumentPage).where(DocumentPage.version_id == vid).order_by(DocumentPage.page_num)
        if page_start is not None:
            query = query.where(DocumentPage.page_num >= page_start)
        if page_end is not None:
            query = query.where(DocumentPage.page_num <= page_end)

        page_rows = (await session.execute(query)).scalars().all()

        pages_out = []
        total_chars = 0
        truncated = False
        for p in page_rows:
            text = p.page_text
            if total_chars + len(text) > max_chars:
                text = text[: max_chars - total_chars]
                pages_out.append(
                    {
                        "page_num": p.page_num,
                        "text": text,
                        "ocr_used": p.ocr_used,
                        "ocr_confidence": p.ocr_confidence,
                    }
                )
                total_chars += len(text)
                truncated = True
                break
            pages_out.append(
                {
                    "page_num": p.page_num,
                    "text": text,
                    "ocr_used": p.ocr_used,
                    "ocr_confidence": p.ocr_confidence,
                }
            )
            total_chars += len(text)

    return json.dumps(
        {
            "doc_id": str(doc.doc_id),
            "version_id": str(vid),
            "title": doc.title,
            "source": "pages",
            "page_count": page_count,
            "pages_returned": len(pages_out),
            "total_chars": total_chars,
            "truncated": truncated,
            "pages": pages_out,
        },
        indent=2,
    )


@mcp.tool()
async def kb_system_health() -> str:
    """Check system health (Postgres, storage). Admin only."""
    _require_admin()

    from sqlalchemy import text

    checks: dict = {}

    async with async_session_factory() as session:
        try:
            await session.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as e:
            checks["postgres"] = f"error: {e}"

    try:
        from harbor_clerk.storage import get_storage

        storage = get_storage()
        storage.bucket_exists("originals")
        checks["storage"] = "ok"
    except Exception as e:
        checks["storage"] = f"error: {e}"

    overall = all(v == "ok" for v in checks.values())
    return json.dumps(
        {
            "status": "healthy" if overall else "degraded",
            "checks": checks,
        },
        indent=2,
    )


def create_mcp_app():
    """Create MCP ASGI apps with auth middleware.

    Returns (header_auth_app, token_path_app, session_manager).

    - header_auth_app: expects ``Authorization: Bearer <key>`` header (mounted at ``/mcp``)
    - token_path_app: expects API key in URL path (mounted at ``/t`` for authless MCP clients)
    - session_manager: must be started via ``async with session_manager.run():``
      in the host application's lifespan
    """
    mcp_http = mcp.streamable_http_app()
    # Dig out the session manager so the host can run it
    session_manager = None
    for route in mcp_http.routes:
        inner = getattr(route, "app", None)
        if hasattr(inner, "session_manager"):
            session_manager = inner.session_manager
            break
    return MCPAuthMiddleware(mcp_http), MCPTokenPathAuth(mcp_http), session_manager
