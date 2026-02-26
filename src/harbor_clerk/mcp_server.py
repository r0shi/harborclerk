"""MCP server — 7 tools for Claude to query the knowledge base."""

import contextvars
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update

from harbor_clerk.api.deps import Principal
from harbor_clerk.auth import API_KEY_PREFIXES, decode_token, hash_api_key
from harbor_clerk.db import async_session_factory
from harbor_clerk.models import (
    ApiKey,
    Chunk,
    Document,
    DocumentVersion,
    IngestionJob,
)
from harbor_clerk.models.enums import JobStage, VersionStatus
from harbor_clerk.config import get_settings
from harbor_clerk.search import hybrid_search

logger = logging.getLogger(__name__)

# Runtime stats for kb_search usage monitoring
_search_stats = {
    "calls": 0,
    "total_k": 0,
    "max_k": 0,
    "cap_hits": 0,
    "pagination_calls": 0,
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
            update(ApiKey)
            .where(ApiKey.key_id == api_key.key_id)
            .values(last_used_at=datetime.now(timezone.utc))
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


@mcp.tool()
async def kb_search(
    query: str,
    k: int = 10,
    offset: int = 0,
    detail: str = "full",
    brief_chars: int = 0,
    doc_id: str | None = None,
) -> str:
    """Search the knowledge base with hybrid FTS + vector search.

    Returns ranked chunks with citations (page numbers, scores).
    Use this as the primary tool to find information.

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

    Pagination: use offset to page through results. Check has_more
    in the response to know if more results exist beyond your window.
    """
    _get_principal()
    settings = get_settings()
    did = uuid.UUID(doc_id) if doc_id else None

    # Clamp parameters
    k = max(1, min(k, settings.mcp_max_k))
    offset = max(0, offset)
    if detail not in ("full", "brief", "compact"):
        detail = "full"

    async with async_session_factory() as session:
        result = await hybrid_search(session, query, k=k, doc_id=did, offset=offset)

    # Resolve brief_chars for brief mode
    effective_brief_chars = brief_chars if brief_chars > 0 else settings.mcp_brief_chars

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
            hit["pages"] = (
                f"{h.page_start}-{h.page_end}"
                if h.page_end != h.page_start
                else str(h.page_start)
            )
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

    resp: dict = {
        "hits": hits,
        "total_candidates": result.total_candidates,
        "has_more": offset + k < result.total_candidates,
    }
    if result.possible_conflict:
        resp["possible_conflict"] = True
        resp["conflict_sources"] = [
            {"doc_id": cs.doc_id, "version_id": cs.version_id, "title": cs.title}
            for cs in result.conflict_sources
        ]

    # Runtime stats
    _search_stats["calls"] += 1
    _search_stats["total_k"] += k
    _search_stats["max_k"] = max(_search_stats["max_k"], k)
    if k >= settings.mcp_max_k:
        _search_stats["cap_hits"] += 1
    if offset > 0:
        _search_stats["pagination_calls"] += 1
    _search_stats[f"detail_{detail}"] += 1

    if _search_stats["calls"] % _STATS_LOG_INTERVAL == 0:
        n = _search_stats["calls"]
        logger.info(
            "kb_search stats (%d calls): avg_k=%.0f, max_k=%d, cap_hit_rate=%.0f%%, "
            "pagination_rate=%.0f%%, detail: full=%d brief=%d compact=%d",
            n,
            _search_stats["total_k"] / n,
            _search_stats["max_k"],
            100 * _search_stats["cap_hits"] / n,
            100 * _search_stats["pagination_calls"] / n,
            _search_stats["detail_full"],
            _search_stats["detail_brief"],
            _search_stats["detail_compact"],
        )

    return json.dumps(resp, indent=2)


@mcp.tool()
async def kb_read_passages(
    chunk_ids: list[str],
    include_context: bool = False,
) -> str:
    """Read specific passages by chunk ID. Use after kb_search to get full text.

    Set include_context=True to also get the surrounding chunks.
    """
    _get_principal()
    uuids = [uuid.UUID(cid) for cid in chunk_ids]

    async with async_session_factory() as session:
        result = await session.execute(select(Chunk).where(Chunk.chunk_id.in_(uuids)))
        chunks = {c.chunk_id: c for c in result.scalars().all()}

        # Doc titles
        doc_ids = {c.doc_id for c in chunks.values()}
        docs_result = await session.execute(
            select(Document).where(Document.doc_id.in_(list(doc_ids)))
        )
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
async def kb_get_document(doc_id: str) -> str:
    """Get document details including all versions and their status."""
    _get_principal()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(select(Document).where(Document.doc_id == did))
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        versions = []
        for v in doc.versions or []:
            jobs_result = await session.execute(
                select(IngestionJob).where(IngestionJob.version_id == v.version_id)
            )
            jobs = [
                {"stage": j.stage.value, "status": j.status.value, "error": j.error}
                for j in jobs_result.scalars().all()
            ]
            versions.append(
                {
                    "version_id": str(v.version_id),
                    "status": v.status.value,
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
            "latest_version_id": str(doc.latest_version_id)
            if doc.latest_version_id
            else None,
            "versions": versions,
        },
        indent=2,
    )


@mcp.tool()
async def kb_list_recent(limit: int = 20) -> str:
    """List recently updated documents."""
    _get_principal()

    async with async_session_factory() as session:
        result = await session.execute(
            select(Document)
            .where(Document.status == "active")
            .order_by(Document.updated_at.desc())
            .limit(min(limit, 100))
        )
        docs = result.scalars().all()

    items = []
    for doc in docs:
        latest_status = None
        if doc.latest_version_id and doc.versions:
            for v in doc.versions:
                if v.version_id == doc.latest_version_id:
                    latest_status = v.status.value
                    break
        items.append(
            {
                "doc_id": str(doc.doc_id),
                "title": doc.title,
                "status": doc.status,
                "latest_version_status": latest_status,
                "version_count": len(doc.versions) if doc.versions else 0,
                "updated_at": doc.updated_at.isoformat(),
            }
        )

    return json.dumps({"documents": items}, indent=2)


@mcp.tool()
async def kb_ingest_status(doc_id: str) -> str:
    """Check ingestion pipeline status for a document's latest version."""
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

        ver_result = await session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == vid)
        )
        version = ver_result.scalar_one()

        jobs_result = await session.execute(
            select(IngestionJob)
            .where(IngestionJob.version_id == vid)
            .order_by(IngestionJob.created_at)
        )
        jobs = [
            {
                "stage": j.stage.value,
                "status": j.status.value,
                "progress": f"{j.progress_current}/{j.progress_total}"
                if j.progress_total
                else None,
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
    """Re-run the full ingestion pipeline for a document. Admin only."""
    _require_admin()
    did = uuid.UUID(doc_id)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Document).where(Document.doc_id == did, Document.status == "active")
        )
        doc = result.scalar_one_or_none()
        if doc is None:
            return json.dumps({"error": "Document not found"})

        vid = doc.latest_version_id
        if vid is None and doc.versions:
            vid = doc.versions[-1].version_id
        if vid is None:
            return json.dumps({"error": "No version to reprocess"})

        ver_result = await session.execute(
            select(DocumentVersion).where(DocumentVersion.version_id == vid)
        )
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
    """Create the MCP ASGI app wrapped with auth middleware.

    Returns (asgi_app, session_manager) — the session_manager must be
    started via ``async with session_manager.run():`` in the host
    application's lifespan, since FastAPI does not propagate lifespan
    events to mounted sub-apps.
    """
    mcp_http = mcp.streamable_http_app()
    # Dig out the session manager so the host can run it
    session_manager = None
    for route in mcp_http.routes:
        inner = getattr(route, "app", None)
        if hasattr(inner, "session_manager"):
            session_manager = inner.session_manager
            break
    return MCPAuthMiddleware(mcp_http), session_manager
